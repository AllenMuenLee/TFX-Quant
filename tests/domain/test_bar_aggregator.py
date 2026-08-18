from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from tfx_quant.domain.bar import CandleColor
from tfx_quant.domain.bar_aggregator import BarAggregator, CandleStreakCounter
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.tick import Tick
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_WEDNESDAY = date(2026, 9, 16)
_THURSDAY = date(2026, 9, 17)


def _entry() -> InstrumentMasterEntry:
    return InstrumentMasterEntry(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        vendor_symbol="TXFU6",
        broker_product_code="TXF",
        tick_size=Decimal("1"),
        multiplier=Decimal("200"),
        day_session_start=time(8, 45),
        day_session_end=time(13, 45),
        night_session_start=time(15, 0),
        night_session_end=time(5, 0),
        expiry_date=date(2026, 9, 16),
        tradable=True,
    )


def _ts(d: date, hour: int, minute: int, second: int = 0) -> Timestamp:
    return Timestamp(datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=TAIPEI_TZ))


def _tick(
    at: Timestamp, price: str, *, size: int = 1, cumulative_volume: int
) -> Tick:
    return Tick(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        at=at,
        price=Price(Decimal(price)),
        size=size,
        cumulative_volume=cumulative_volume,
    )


def _aggregator() -> BarAggregator:
    return BarAggregator(
        instrument=_INSTRUMENT, contract=_CONTRACT, entry=_entry(), calendar=TradingCalendar()
    )


def test_ohlcv_correctness_within_one_bar() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=1))
    agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 0), "17510", cumulative_volume=3, size=2))
    agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 10), "17490", cumulative_volume=4))
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.open.amount == Decimal("17500")
    assert forming.high.amount == Decimal("17510")
    assert forming.low.amount == Decimal("17490")
    assert forming.close.amount == Decimal("17490")
    assert forming.volume == 4  # 1 + 2 + 1
    assert forming.start.value.time() == time(8, 45)
    assert forming.end.value.time() == time(9, 45)


def test_bar_closes_and_emits_when_boundary_crossed() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=1))
    closed = agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 50), "17600", cumulative_volume=2))
    assert len(closed) == 1
    bar = closed[0]
    assert bar.start.value.time() == time(8, 45)
    assert bar.end.value.time() == time(9, 45)
    assert bar.open.amount == Decimal("17500")
    assert bar.close.amount == Decimal("17500")  # the second tick opens the NEXT bar
    # the new forming bar was opened by the boundary-crossing tick
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.start.value.time() == time(9, 45)
    assert forming.open.amount == Decimal("17600")


def test_tick_exactly_at_boundary_belongs_to_next_bar() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=1))
    closed = agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 45, 0), "17600", cumulative_volume=2))
    assert len(closed) == 1
    assert closed[0].start.value.time() == time(8, 45)
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.start.value.time() == time(9, 45)  # not 08:45 — half-open [open, close)


def test_duplicate_tick_is_dropped() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=5))
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 51), "17999", cumulative_volume=5))  # duplicate push
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.close.amount == Decimal("17500")
    assert forming.high.amount == Decimal("17500")
    assert forming.volume == 1


def test_out_of_order_tick_is_dropped() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=10))
    # a late/out-of-order push whose cumulative volume regresses
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 51), "16000", cumulative_volume=9))
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.close.amount == Decimal("17500")
    assert forming.low.amount == Decimal("17500")
    assert forming.volume == 1


def test_late_tick_after_bar_already_closed_is_dropped() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=1))
    closed = agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 50), "17600", cumulative_volume=2))
    assert len(closed) == 1
    # a tick that arrives late, timestamped inside the already-closed 08:45 bar
    late = agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 30), "99999", cumulative_volume=3))
    assert late == []
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.open.amount == Decimal("17600")  # unaffected by the late tick


def test_midnight_crossing_closes_and_opens_across_dates() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 23, 10), "17700", cumulative_volume=1))
    closed = agg.on_tick(_tick(_ts(_THURSDAY, 0, 15), "17650", cumulative_volume=2))
    assert len(closed) == 1
    bar = closed[0]
    assert bar.start.value.date() == _WEDNESDAY
    assert bar.start.value.time() == time(23, 0)
    assert bar.end.value.date() == _THURSDAY
    assert bar.end.value.time() == time(0, 0)
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.start.value.date() == _THURSDAY
    assert forming.start.value.time() == time(0, 0)


def test_no_trade_interval_bar_is_never_synthesized() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=1))
    # no ticks at all during the 09:45-10:45 slot — jump straight to 11:00
    closed = agg.on_tick(_tick(_ts(_WEDNESDAY, 11, 0), "17800", cumulative_volume=2))
    assert len(closed) == 1
    assert closed[0].start.value.time() == time(8, 45)
    forming = agg.forming_bar_snapshot()
    assert forming is not None
    assert forming.start.value.time() == time(10, 45)  # the 09:45 slot was skipped, not faked


def test_on_clock_closes_forming_bar_without_a_new_tick() -> None:
    agg = _aggregator()
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "17500", cumulative_volume=1))
    closed = agg.on_clock(_ts(_WEDNESDAY, 9, 46))
    assert len(closed) == 1
    assert closed[0].close.amount == Decimal("17500")
    assert agg.forming_bar_snapshot() is None  # on_clock never opens a new forming bar


def test_on_clock_does_not_fabricate_bars_with_no_forming_state() -> None:
    agg = _aggregator()
    assert agg.on_clock(_ts(_WEDNESDAY, 9, 46)) == []
    assert agg.forming_bar_snapshot() is None


def test_candle_streak_counter_tracks_consecutive_same_color() -> None:
    counter = CandleStreakCounter()
    agg = _aggregator()
    # bar A (08:45-09:45): open 100, close 105 -> RED
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "100", cumulative_volume=1))
    agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 0), "105", cumulative_volume=2))
    # this tick crosses the boundary: closes bar A, opens bar B
    for bar in agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 50), "110", cumulative_volume=3)):
        counter.on_bar_closed(bar)
    assert counter.color is CandleColor.RED
    assert counter.length == 1

    # bar B (09:45-10:45): open 110, close 115 -> RED
    agg.on_tick(_tick(_ts(_WEDNESDAY, 10, 0), "115", cumulative_volume=4))
    for bar in agg.on_tick(_tick(_ts(_WEDNESDAY, 10, 50), "120", cumulative_volume=5)):
        counter.on_bar_closed(bar)
    assert counter.color is CandleColor.RED
    assert counter.length == 2


def test_candle_streak_counter_doji_resets_streak() -> None:
    counter = CandleStreakCounter()
    agg = _aggregator()
    # bar A (08:45-09:45): open 100, close 105 -> RED, streak = 1
    agg.on_tick(_tick(_ts(_WEDNESDAY, 8, 50), "100", cumulative_volume=1))
    agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 0), "105", cumulative_volume=2))
    for bar in agg.on_tick(_tick(_ts(_WEDNESDAY, 9, 50), "110", cumulative_volume=3)):
        counter.on_bar_closed(bar)
    assert counter.length == 1

    # bar B (09:45-10:45): open 110, dips to 100, closes back at 110 -> DOJI, resets
    agg.on_tick(_tick(_ts(_WEDNESDAY, 10, 0), "100", cumulative_volume=4))
    agg.on_tick(_tick(_ts(_WEDNESDAY, 10, 30), "110", cumulative_volume=5))
    closed = agg.on_tick(_tick(_ts(_WEDNESDAY, 10, 50), "120", cumulative_volume=6))
    assert closed[0].candle_color is CandleColor.DOJI
    counter.on_bar_closed(closed[0])
    assert counter.length == 0
    assert counter.color is None
