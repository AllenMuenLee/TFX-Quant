from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import (
    BarConflictAudit,
    BarDataSource,
    BarPeriod,
    BarRecord,
    ContinuitySegment,
    MarketSession,
    continuous_segments,
    rolling_two_month_start,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidBarRecordError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)


def _ts(y: int, m: int, d: int, hh: int, mm: int) -> Timestamp:
    return Timestamp(datetime(y, m, d, hh, mm, tzinfo=TAIPEI_TZ))


def _bar(start: Timestamp, end: Timestamp, price: str = "17500") -> Bar:
    return Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        open=Price(Decimal(price)),
        high=Price(Decimal(price)),
        low=Price(Decimal(price)),
        close=Price(Decimal(price)),
        volume=1,
        start=start,
        end=end,
    )


def _record(start: Timestamp, end: Timestamp, **overrides: object) -> BarRecord:
    defaults: dict[str, object] = {
        "bar": _bar(start, end),
        "period": BarPeriod.SIXTY_MINUTE,
        "trading_day": start.value.date(),
        "session": MarketSession.DAY,
        "source": BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME,
        "is_gap_recovery": False,
        "created_at": start,
        "updated_at": start,
    }
    defaults.update(overrides)
    return BarRecord(**defaults)  # type: ignore[arg-type]


# -- rolling_two_month_start ------------------------------------------------------


def test_rolling_two_month_start_worked_example() -> None:
    assert rolling_two_month_start(date(2026, 8, 18)) == date(2026, 6, 18)


def test_rolling_two_month_start_clamps_at_shorter_target_month() -> None:
    # June has no 31st.
    assert rolling_two_month_start(date(2026, 8, 31)) == date(2026, 6, 30)


def test_rolling_two_month_start_crosses_year_boundary() -> None:
    assert rolling_two_month_start(date(2026, 1, 15)) == date(2025, 11, 15)


def test_rolling_two_month_start_leap_year_clamp() -> None:
    # April 30 - 2 months = "February 30", clamped to Feb 29 in a leap year (2028).
    assert rolling_two_month_start(date(2028, 4, 30)) == date(2028, 2, 29)


def test_rolling_two_month_start_non_leap_year_clamp() -> None:
    # Same case one year later — 2027 is not a leap year, so Feb only has 28 days.
    assert rolling_two_month_start(date(2027, 4, 30)) == date(2027, 2, 28)


def test_rolling_two_month_start_december_crossing() -> None:
    assert rolling_two_month_start(date(2026, 12, 31)) == date(2026, 10, 31)


# -- BarRecord ---------------------------------------------------------------------


def test_bar_record_identity_fields() -> None:
    start = _ts(2026, 9, 16, 8, 45)
    end = _ts(2026, 9, 16, 9, 45)
    record = _record(start, end)
    assert record.identity == (_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, start)


def test_bar_record_rejects_revision_below_one() -> None:
    start = _ts(2026, 9, 16, 8, 45)
    end = _ts(2026, 9, 16, 9, 45)
    with pytest.raises(InvalidBarRecordError):
        _record(start, end, revision=0)


def test_bar_record_rejects_updated_before_created() -> None:
    start = _ts(2026, 9, 16, 8, 45)
    end = _ts(2026, 9, 16, 9, 45)
    earlier = start
    later = _ts(2026, 9, 16, 9, 0)
    with pytest.raises(InvalidBarRecordError):
        _record(start, end, created_at=later, updated_at=earlier)


# -- continuous_segments ------------------------------------------------------------


def test_continuous_segments_empty_input() -> None:
    assert continuous_segments([]) == []


def test_continuous_segments_single_record_is_one_segment() -> None:
    start = _ts(2026, 9, 16, 8, 45)
    end = _ts(2026, 9, 16, 9, 45)
    record = _record(start, end)
    segments = continuous_segments([record])
    assert segments == [ContinuitySegment(bars=(record.bar,))]


def test_continuous_segments_merges_adjacent_bars() -> None:
    r1 = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    r2 = _record(_ts(2026, 9, 16, 9, 45), _ts(2026, 9, 16, 10, 45))
    segments = continuous_segments([r1, r2])
    assert len(segments) == 1
    assert segments[0].bars == (r1.bar, r2.bar)
    assert segments[0].start == r1.bar.start
    assert segments[0].end == r2.bar.end


def test_continuous_segments_breaks_on_a_missing_boundary() -> None:
    r1 = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    # r2 skips the 09:45-10:45 bar entirely — a real gap.
    r2 = _record(_ts(2026, 9, 16, 10, 45), _ts(2026, 9, 16, 11, 45))
    segments = continuous_segments([r1, r2])
    assert len(segments) == 2
    assert segments[0].bars == (r1.bar,)
    assert segments[1].bars == (r2.bar,)


def test_continuity_segment_rejects_empty_bars() -> None:
    with pytest.raises(InvalidBarRecordError):
        ContinuitySegment(bars=())


# -- BarConflictAudit ---------------------------------------------------------------


def test_bar_conflict_audit_accepts_matching_identities() -> None:
    start = _ts(2026, 9, 16, 8, 45)
    end = _ts(2026, 9, 16, 9, 45)
    existing = _record(start, end, bar=_bar(start, end, price="17500"))
    incoming = _record(
        start,
        end,
        bar=_bar(start, end, price="17600"),
        source=BarDataSource.BACKFILLED_FROM_YFINANCE,
    )
    audit = BarConflictAudit(existing=existing, incoming=incoming, detected_at=Timestamp.now())
    assert audit.existing is existing
    assert audit.incoming is incoming


def test_bar_conflict_audit_rejects_mismatched_identities() -> None:
    existing = _record(_ts(2026, 9, 16, 8, 45), _ts(2026, 9, 16, 9, 45))
    incoming = _record(
        _ts(2026, 9, 16, 9, 45),
        _ts(2026, 9, 16, 10, 45),
        source=BarDataSource.BACKFILLED_FROM_YFINANCE,
    )
    with pytest.raises(InvalidBarRecordError):
        BarConflictAudit(existing=existing, incoming=incoming, detected_at=Timestamp.now())
