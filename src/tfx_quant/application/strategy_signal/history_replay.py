"""Deterministic history replay used by the simulation execution path."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from tfx_quant.application.ports.market_event_repository import TradePrint
from tfx_quant.domain.bar_record import BarRecord
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind
from tfx_quant.domain.strategy_signal_engine import (
    EngineConfig,
    PositionSide,
    StrategyDecision,
    StrategySignalEngine,
)
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True)
class HistoricalTrade:
    record: BarRecord
    decision: StrategyDecision
    side: Side
    quantity: int


@dataclass(frozen=True)
class HistoricalReplay:
    """Replay output plus the fully warmed strategy state after assumed fills."""

    trades: tuple[HistoricalTrade, ...]
    engine: StrategySignalEngine | None
    review_bars: tuple[BarRecord, ...]


class TradeTape(Protocol):
    """Persisted MXF match prices of one contract, for intrabar exits."""

    def first_beyond(
        self,
        contract: ContractMonth,
        *,
        since: TradePrint | Timestamp,
        before: Timestamp,
        at_or_below: Decimal,
        at_or_above: Decimal,
    ) -> TradePrint | None: ...

    def last_at_or_before(
        self, contract: ContractMonth, *, since: TradePrint | Timestamp, at: Timestamp
    ) -> TradePrint | None: ...


def replay_history(
    records: Sequence[BarRecord],
    next_close: Callable[[Timestamp], Timestamp],
    config: EngineConfig | None = None,
    tape: TradeTape | None = None,
) -> tuple[HistoricalTrade, ...]:
    """Start flat, assume immediate fills at signal prices, restart after unknown data.

    Session breaks preserve state; missing/incomplete bars reset the replay's warmup,
    and the replay itself only while flat — a held position keeps its stop, profit and
    04:55 exits across the hole. With a `tape`, a held position is checked against
    every recorded trade exactly as the live service does: a stop or profit exit fills
    at the first crossing trade, and 04:55 flattens at the last trade before it.
    The returned candidates do not cause side effects by themselves; TEST
    composition explicitly hands them to ``HistoricalSimulationService`` for execution.
    """
    return replay_history_state(records, next_close, config, tape).trades


def replay_history_state(
    records: Sequence[BarRecord],
    next_close: Callable[[Timestamp], Timestamp],
    config: EngineConfig | None = None,
    tape: TradeTape | None = None,
) -> HistoricalReplay:
    """Replay history and retain the final warmed engine for simulation continuation."""
    config = config or EngineConfig()
    results: list[HistoricalTrade] = []
    engine: StrategySignalEngine | None = None
    previous: BarRecord | None = None
    review_records: list[BarRecord] = []

    def trade_exits(
        record: BarRecord, since: TradePrint | Timestamp, before: Timestamp
    ) -> TradePrint | Timestamp:
        """Feed every trade in `[since, before)` that could move an exit; return the
        position to resume from."""
        assert engine is not None
        if tape is None:
            return since
        bar = record.bar
        while (band := engine.exit_watch_prices()) is not None:
            start = since.at if isinstance(since, TradePrint) else since
            if start.value < bar.start.value:
                # Trades in a hole before this bar: no recorded range to rule them out.
                window_end = bar.start if bar.start.value < before.value else before
            elif bar.low.amount > band[0] and bar.high.amount < band[1]:
                break  # the bar's own range never reaches either price
            else:
                window_end = before
            hit = tape.first_beyond(
                bar.contract,
                since=since,
                before=window_end,
                at_or_below=band[0],
                at_or_above=band[1],
            )
            if hit is None:
                if window_end.value < before.value:
                    since = window_end
                    continue
                stop = engine.stop_loss_price
                long = engine.position_side is PositionSide.LONG
                if (
                    start.value >= bar.start.value
                    and stop is not None
                    and (bar.low.amount <= stop if long else bar.high.amount >= stop)
                ):
                    # A bar without recorded trades (manual history) that reached the
                    # stop: it filled at the stop, or at the open if it opened beyond.
                    price = min(bar.open.amount, stop) if long else max(bar.open.amount, stop)
                    accept(
                        record,
                        engine.on_trade_price(
                            price, before, has_active_order=False, position_state_uncertain=False
                        ),
                    )
                break
            accept(
                record,
                engine.on_trade_price(
                    hit.price, hit.at, has_active_order=False, position_state_uncertain=False
                ),
            )
            since = hit
        return since

    def accept(record: BarRecord, decision: StrategyDecision) -> None:
        if not decision.passed or decision.signal_kind is None:
            return
        assert engine is not None and decision.current_price is not None
        if decision.signal_kind is SignalKind.EXIT_ALL:
            side = Side.SELL if decision.position_side is PositionSide.LONG else Side.BUY
            quantity = decision.position_lots
        else:
            side = (
                Side.BUY
                if decision.signal_kind in (SignalKind.ENTER_LONG, SignalKind.ADD_LONG)
                else Side.SELL
            )
            quantity = 1
        results.append(HistoricalTrade(record, decision, side, quantity))
        # Keep replay decisions stateful. TEST composition separately persists the same
        # assumed fills through the simulator; production merely displays candidates.
        engine.on_fill_confirmed(
            side=side, price=decision.current_price, quantity=quantity, at=decision.at
        )

    for record in records:
        if record.bar.instrument is not Instrument.MXF or (
            previous is not None and previous.bar.contract != record.bar.contract
        ):
            engine, previous = None, None
            review_records = []
        if record.bar.instrument is not Instrument.MXF:
            continue
        if not record.is_complete or (
            previous is not None and next_close(previous.bar.end) != record.bar.end
        ):
            review_records = []
            if engine is not None and engine.position_side is not PositionSide.FLAT:
                # Unknown data restarts the warmup, never the position: the open lots
                # and their stop stay live until an exit actually closes them.
                engine.reset_market_history()
            else:
                engine, previous = None, None
        if not record.is_complete and engine is None:
            continue
        if engine is None:
            engine = StrategySignalEngine(
                instrument=record.bar.instrument, contract=record.bar.contract, config=config
            )
        if previous is not None:
            # The live order of events between two closes: trades, the 04:55 clock
            # (priced at the last trade before it), more trades, then the next close.
            since: TradePrint | Timestamp = previous.bar.end
            day = previous.bar.end.value.date()
            while day <= record.bar.end.value.date():
                tick = Timestamp(
                    datetime.combine(
                        day, config.eod_flatten_local_time, tzinfo=record.bar.end.value.tzinfo
                    )
                )
                if previous.bar.end.value < tick.value <= record.bar.end.value:
                    since = trade_exits(record, since, tick)
                    last = (
                        None
                        if tape is None or engine.position_side is PositionSide.FLAT
                        else tape.last_at_or_before(record.bar.contract, since=since, at=tick)
                    )
                    if last is not None:
                        accept(
                            record,
                            engine.on_trade_price(
                                last.price,
                                last.at,
                                has_active_order=False,
                                position_state_uncertain=False,
                            ),
                        )
                        since = last
                    accept(
                        previous,
                        engine.on_clock_tick(
                            tick, has_active_order=False, position_state_uncertain=False
                        ),
                    )
                day += timedelta(days=1)
            trade_exits(record, since, record.bar.end)
        accept(
            record,
            engine.on_bar_closed(
                record.bar,
                data_reliable=record.is_complete,
                has_active_order=False,
                position_state_uncertain=False,
            ),
        )
        previous = record
        if record.is_complete:
            review_records.append(record)
        else:
            # An unreviewed close may trigger an exit but never feeds the next warmup.
            engine.reset_market_history()
    return HistoricalReplay(tuple(results), engine, tuple(review_records))


__all__ = [
    "HistoricalReplay",
    "HistoricalTrade",
    "TradeTape",
    "replay_history",
    "replay_history_state",
]
