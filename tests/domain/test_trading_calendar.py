from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from tfx_quant.domain.bar_record import MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar

_WEDNESDAY = date(2026, 9, 16)  # a normal trading day (TXF 202609's 3rd-Wednesday expiry)
_SATURDAY = date(2026, 9, 19)
_HOLIDAY = date(2026, 9, 25)  # a weekday, marked as a holiday for these tests


def _entry(**overrides: object) -> InstrumentMasterEntry:
    defaults: dict[str, object] = {
        "instrument": Instrument.TXF,
        "contract": ContractMonth(year=2026, month=9),
        "vendor_symbol": "TXFU6",
        "broker_product_code": "TXF",
        "tick_size": Decimal("1"),
        "multiplier": Decimal("200"),
        "day_session_start": time(8, 45),
        "day_session_end": time(13, 45),
        "night_session_start": time(15, 0),
        "night_session_end": time(5, 0),
        "expiry_date": date(2026, 9, 16),
        "tradable": True,
    }
    defaults.update(overrides)
    return InstrumentMasterEntry(**defaults)  # type: ignore[arg-type]


def _ts(d: date, hour: int, minute: int) -> Timestamp:
    from datetime import datetime

    return Timestamp(datetime(d.year, d.month, d.day, hour, minute, tzinfo=TAIPEI_TZ))


def test_is_trading_day_true_for_plain_weekday() -> None:
    calendar = TradingCalendar()
    assert calendar.is_trading_day(_WEDNESDAY)


def test_is_trading_day_false_for_weekend() -> None:
    calendar = TradingCalendar()
    assert not calendar.is_trading_day(_SATURDAY)


def test_is_trading_day_false_for_holiday() -> None:
    calendar = TradingCalendar(holidays=frozenset({_HOLIDAY}))
    assert not calendar.is_trading_day(_HOLIDAY)


def test_session_end_for_applies_earlier_override() -> None:
    calendar = TradingCalendar(early_closes={_WEDNESDAY: time(11, 15)})
    assert calendar.session_end_for(_WEDNESDAY, time(13, 45)) == time(11, 15)


def test_session_end_for_ignores_override_not_earlier_than_base() -> None:
    calendar = TradingCalendar(early_closes={_WEDNESDAY: time(14, 0)})
    assert calendar.session_end_for(_WEDNESDAY, time(13, 45)) == time(13, 45)


def test_session_end_for_no_override_returns_base() -> None:
    calendar = TradingCalendar()
    assert calendar.session_end_for(_WEDNESDAY, time(13, 45)) == time(13, 45)


def test_day_session_bar_boundaries_are_five_clean_hourly_bars() -> None:
    calendar = TradingCalendar()
    boundaries = calendar.bar_boundaries(_WEDNESDAY, time(8, 45), time(13, 45))
    labels = [(open_.value.time(), close.value.time()) for open_, close in boundaries]
    assert labels == [
        (time(8, 45), time(9, 45)),
        (time(9, 45), time(10, 45)),
        (time(10, 45), time(11, 45)),
        (time(11, 45), time(12, 45)),
        (time(12, 45), time(13, 45)),
    ]
    assert all(open_.value.date() == _WEDNESDAY for open_, _ in boundaries)


def test_night_session_bar_boundaries_cross_midnight_cleanly() -> None:
    calendar = TradingCalendar()
    boundaries = calendar.bar_boundaries(_WEDNESDAY, time(15, 0), time(5, 0))
    assert len(boundaries) == 14
    assert boundaries[0][0].value.time() == time(15, 0)
    assert boundaries[0][0].value.date() == _WEDNESDAY
    # the last pre-midnight boundary and the first post-midnight boundary are contiguous
    pre_midnight = boundaries[8]
    post_midnight = boundaries[9]
    assert pre_midnight[0].value.time() == time(23, 0)
    assert pre_midnight[1] == post_midnight[0]
    assert post_midnight[0].value.date() == date(2026, 9, 17)
    assert post_midnight[0].value.time() == time(0, 0)
    last_open, last_close = boundaries[-1]
    assert last_open.value.time() == time(4, 0)
    assert last_close.value.time() == time(5, 0)
    assert last_close.value.date() == date(2026, 9, 17)


def test_early_close_truncates_final_bar() -> None:
    calendar = TradingCalendar(early_closes={_WEDNESDAY: time(11, 15)})
    boundaries = calendar.bar_boundaries(_WEDNESDAY, time(8, 45), time(13, 45))
    assert [(o.value.time(), c.value.time()) for o, c in boundaries] == [
        (time(8, 45), time(9, 45)),
        (time(9, 45), time(10, 45)),
        (time(10, 45), time(11, 15)),
    ]


def test_boundary_containing_finds_day_session_bar() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    at = _ts(_WEDNESDAY, 9, 10)
    boundary = calendar.boundary_containing(at, entry)
    assert boundary is not None
    open_ts, close_ts = boundary
    assert open_ts.value.time() == time(8, 45)
    assert close_ts.value.time() == time(9, 45)


def test_boundary_containing_none_outside_any_session() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    at = _ts(_WEDNESDAY, 7, 0)  # between night end (05:00) and day start (08:45)
    assert calendar.boundary_containing(at, entry) is None


def test_resolve_tick_timestamp_day_session() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    received_at = _ts(_WEDNESDAY, 9, 30)
    resolved = calendar.resolve_tick_timestamp(time(9, 29, 55), received_at, entry)
    assert resolved is not None
    assert resolved.value.date() == _WEDNESDAY
    assert resolved.value.time() == time(9, 29, 55)


def test_resolve_tick_timestamp_night_session_before_midnight() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    received_at = _ts(_WEDNESDAY, 20, 0)
    resolved = calendar.resolve_tick_timestamp(time(20, 0), received_at, entry)
    assert resolved is not None
    assert resolved.value.date() == _WEDNESDAY


def test_resolve_tick_timestamp_night_session_after_midnight_belongs_to_next_calendar_date() -> (
    None
):
    calendar = TradingCalendar()
    entry = _entry()
    next_day = date(2026, 9, 17)
    received_at = _ts(next_day, 2, 5)
    resolved = calendar.resolve_tick_timestamp(time(2, 0), received_at, entry)
    assert resolved is not None
    assert resolved.value.date() == next_day
    assert resolved.value.time() == time(2, 0)


def test_resolve_tick_timestamp_none_outside_any_session() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    received_at = _ts(_WEDNESDAY, 7, 0)
    assert calendar.resolve_tick_timestamp(time(7, 0), received_at, entry) is None


def test_session_context_for_day_session_boundary() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    open_ts = _ts(_WEDNESDAY, 8, 45)
    context = calendar.session_context_for(open_ts, entry)
    assert context == (_WEDNESDAY, MarketSession.DAY)


def test_session_context_for_night_session_boundary_before_midnight() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    open_ts = _ts(_WEDNESDAY, 15, 0)
    context = calendar.session_context_for(open_ts, entry)
    assert context == (_WEDNESDAY, MarketSession.NIGHT)


def test_session_context_for_night_session_after_midnight_belongs_to_prior_trading_day() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    # 00:00 on 9/17 is still part of the night session that opened on 9/16 (_WEDNESDAY).
    open_ts = _ts(date(2026, 9, 17), 0, 0)
    context = calendar.session_context_for(open_ts, entry)
    assert context == (_WEDNESDAY, MarketSession.NIGHT)


def test_session_context_for_none_when_not_a_boundary() -> None:
    calendar = TradingCalendar()
    entry = _entry()
    not_a_boundary = _ts(_WEDNESDAY, 9, 10)  # mid-bar, not an open-label boundary
    assert calendar.session_context_for(not_a_boundary, entry) is None
