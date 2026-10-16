"""History replay against recorded trades: exits fire intrabar, like the live service."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from tfx_quant.application.ports.market_event_repository import TradePrint
from tfx_quant.application.strategy_signal.history_replay import HistoricalTrade, replay_history
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord, MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_signal_engine import EngineConfig
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_CONTRACT = ContractMonth(2026, 10)
_CONFIG = EngineConfig(ma_window=2, flat_lookback=2, flat_threshold_points=Decimal("1"))
# Enters long at 48761 on the 4th close and adds at 48905 on the 5th — the client's
# 10/2 example: average cost 48833, stop basis 48905.
_RISING = ("48500", "48600", "48700", "48761", "48905")


def _at(hour: int, minute: int) -> Timestamp:
    return Timestamp(datetime(2026, 10, 2, hour, minute, tzinfo=TAIPEI_TZ))


def _record(index: int, open_: str, high: str, low: str, close: str) -> BarRecord:
    start = Timestamp(_at(10, 45).value + timedelta(hours=index))
    bar = Bar(
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        open=Price(Decimal(open_)),
        high=Price(Decimal(high)),
        low=Price(Decimal(low)),
        close=Price(Decimal(close)),
        volume=10,
        start=start,
        end=Timestamp(start.value + timedelta(hours=1)),
    )
    return BarRecord(
        bar=bar,
        period=BarPeriod.SIXTY_MINUTE,
        trading_day=date(2026, 10, 2),
        session=MarketSession.DAY,
        source=BarDataSource.LOCAL_YUANTA_REALTIME,
        is_gap_recovery=False,
        created_at=bar.end,
        updated_at=bar.end,
    )


def _rising() -> list[BarRecord]:
    return [
        _record(index, str(Decimal(close) - 5), close, str(Decimal(close) - 5), close)
        for index, close in enumerate(_RISING)
    ]


class _Tape:
    def __init__(self, trades: list[tuple[Timestamp, str]]) -> None:
        self._prints = [
            TradePrint(at, Decimal(price), (at.value.isoformat(), "s", index))
            for index, (at, price) in enumerate(trades)
        ]

    def _after(self, since: TradePrint | Timestamp) -> list[TradePrint]:
        if isinstance(since, TradePrint):
            return [p for p in self._prints if p.position > since.position]
        return [p for p in self._prints if p.at.value >= since.value]

    def first_beyond(
        self,
        contract: ContractMonth,
        *,
        since: TradePrint | Timestamp,
        before: Timestamp,
        at_or_below: Decimal,
        at_or_above: Decimal,
    ) -> TradePrint | None:
        assert contract == _CONTRACT
        return next(
            (
                p
                for p in self._after(since)
                if p.at.value < before.value and (p.price <= at_or_below or p.price >= at_or_above)
            ),
            None,
        )

    def last_at_or_before(
        self, contract: ContractMonth, *, since: TradePrint | Timestamp, at: Timestamp
    ) -> TradePrint | None:
        matching = [p for p in self._after(since) if p.at.value <= at.value]
        return matching[-1] if matching else None


def _next_close(end: Timestamp) -> Timestamp:
    return Timestamp(end.value + timedelta(hours=1))


def _exits(
    trades: tuple[HistoricalTrade, ...],
) -> list[tuple[str, Side, int, Decimal | None, Timestamp]]:
    return [
        (t.decision.rule, t.side, t.quantity, t.decision.current_price, t.decision.at)
        for t in trades
        if t.decision.rule not in ("enter_long", "add_long")
    ]


def test_profit_pullback_exits_both_lots_at_the_first_trade_past_30_percent() -> None:
    # Peak 49492 = 659 points over the 48833 average; 30% back is 49294.3.
    records = [*_rising(), _record(5, "49205", "49492", "49205", "49210")]
    tape = _Tape(
        [
            (_at(15, 50), "49205"),
            (_at(16, 0), "49492"),
            (_at(16, 10), "49300"),
            (_at(16, 20), "49294"),
            (_at(16, 30), "49205"),
        ]
    )

    trades = replay_history(records, _next_close, _CONFIG, tape)

    assert _exits(trades) == [("profit_pullback", Side.SELL, 2, Decimal("49294"), _at(16, 20))]


def test_stop_fills_at_the_first_trade_300_points_against_the_last_lot() -> None:
    records = [*_rising(), _record(5, "48905", "48910", "48500", "48530")]
    tape = _Tape([(_at(16, 0), "48700"), (_at(16, 10), "48605"), (_at(16, 20), "48500")])

    trades = replay_history(records, _next_close, _CONFIG, tape)

    assert _exits(trades) == [("stop_loss", Side.SELL, 2, Decimal("48605"), _at(16, 10))]


def test_stop_fires_on_trades_recorded_inside_a_missing_bar() -> None:
    # The 15:45 bar is missing (e.g. a quote re-login); its trades still count.
    records = [*_rising(), _record(6, "48600", "48650", "48550", "48600")]
    tape = _Tape([(_at(16, 5), "48800"), (_at(16, 34), "48604"), (_at(17, 0), "48600")])

    trades = replay_history(records, _next_close, _CONFIG, tape)

    assert _exits(trades) == [("stop_loss", Side.SELL, 2, Decimal("48604"), _at(16, 34))]


def test_bar_without_recorded_trades_stops_at_the_stop_price() -> None:
    records = [*_rising(), _record(5, "48900", "48910", "48500", "48530")]

    trades = replay_history(records, _next_close, _CONFIG, _Tape([]))

    assert _exits(trades) == [("stop_loss", Side.SELL, 2, Decimal("48605"), _at(16, 45))]


def test_eod_flatten_uses_the_last_trade_before_the_flatten_time() -> None:
    config = replace(
        _CONFIG, eod_flatten_local_time=time(16, 15), entry_gate_local_time=time(23, 0)
    )
    records = [*_rising(), _record(5, "48905", "49100", "48900", "49050")]
    tape = _Tape([(_at(16, 5), "49000"), (_at(16, 14), "49010"), (_at(16, 20), "49100")])

    trades = replay_history(records, _next_close, config, tape)

    assert _exits(trades) == [("eod_flatten", Side.SELL, 2, Decimal("49010"), _at(16, 15))]


def test_without_a_tape_exits_are_still_evaluated_at_the_close() -> None:
    records = [*_rising(), _record(5, "48905", "48910", "48500", "48530")]

    trades = replay_history(records, _next_close, _CONFIG)

    assert _exits(trades) == [("stop_loss", Side.SELL, 2, Decimal("48530"), _at(16, 45))]
