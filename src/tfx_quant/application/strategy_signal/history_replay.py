"""Deterministic history replay used by the simulation execution path."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tfx_quant.domain.bar_record import BarRecord
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


def replay_history(
    records: Sequence[BarRecord],
    next_close: Callable[[Timestamp], Timestamp],
    config: EngineConfig | None = None,
) -> tuple[HistoricalTrade, ...]:
    """Start flat, assume immediate fills at signal prices, restart after unknown data.

    Session breaks preserve state; missing/incomplete bars reset the replay and its
    warmup. The returned candidates do not cause side effects by themselves; TEST
    composition explicitly hands them to ``HistoricalSimulationService`` for execution.
    """
    return replay_history_state(records, next_close, config).trades


def replay_history_state(
    records: Sequence[BarRecord],
    next_close: Callable[[Timestamp], Timestamp],
    config: EngineConfig | None = None,
) -> HistoricalReplay:
    """Replay history and retain the final warmed engine for simulation continuation."""
    config = config or EngineConfig()
    results: list[HistoricalTrade] = []
    engine: StrategySignalEngine | None = None
    previous: BarRecord | None = None
    review_records: list[BarRecord] = []

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
        if record.bar.instrument is not Instrument.MXF or not record.is_complete:
            engine, previous = None, None
            review_records = []
            continue
        if previous is not None and (
            previous.bar.contract != record.bar.contract
            or next_close(previous.bar.end) != record.bar.end
        ):
            engine, previous = None, None
            review_records = []
        if engine is None:
            engine = StrategySignalEngine(
                instrument=record.bar.instrument, contract=record.bar.contract, config=config
            )
        if previous is not None:
            day = previous.bar.end.value.date()
            while day <= record.bar.end.value.date():
                tick = Timestamp(
                    datetime.combine(
                        day, config.eod_flatten_local_time, tzinfo=record.bar.end.value.tzinfo
                    )
                )
                if previous.bar.end.value < tick.value <= record.bar.end.value:
                    accept(
                        previous,
                        engine.on_clock_tick(
                            tick, has_active_order=False, position_state_uncertain=False
                        ),
                    )
                day += timedelta(days=1)
        accept(
            record,
            engine.on_bar_closed(
                record.bar,
                data_reliable=True,
                has_active_order=False,
                position_state_uncertain=False,
            ),
        )
        previous = record
        review_records.append(record)
    return HistoricalReplay(tuple(results), engine, tuple(review_records))


__all__ = [
    "HistoricalReplay",
    "HistoricalTrade",
    "replay_history",
    "replay_history_state",
]
