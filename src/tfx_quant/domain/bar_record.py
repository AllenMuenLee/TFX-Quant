"""BarRecord — the persisted-history wrapper around a closed `Bar`.

`Bar` (see `domain/bar.py`) stays the pure aggregation output used across the whole
codebase (events, streak counting, UI). `BarRecord` is a separate value object used only
by the persistence path (this Feature 04 extension): it carries the extra fields the
implementation prompt requires storing — 週期 (period), 交易日 (trading_day), session,
資料來源 (source), and the created/updated audit timestamps — without forcing every
existing `Bar` construction site to know about them.

`source` is always `BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME` — this software has no
vendor historical-bar API to call (see `docs/adr/0006-market-data-and-bar-aggregation.md`
decision 7) and must never present its own self-aggregated bars as if they were an
official Yuanta historical product.
"""

from __future__ import annotations

import calendar as _calendar_module
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from tfx_quant.domain.bar import Bar
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidBarRecordError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.timestamp import Timestamp


class BarPeriod(StrEnum):
    """The aggregation period a `BarRecord` was closed at. Only one period is ever
    produced today (60-minute bars — see `domain/bar_aggregator.py`), but this stays an
    explicit field (not a hardcoded assumption) because it is part of the bar's unique
    identity per the implementation prompt's own wording ("商品、契約月份、週期...")."""

    SIXTY_MINUTE = "60m"


class MarketSession(StrEnum):
    """Which of an instrument's two daily sessions (see `InstrumentMasterEntry`) a bar
    belongs to."""

    DAY = "DAY"
    NIGHT = "NIGHT"


class BarDataSource(StrEnum):
    AGGREGATED_FROM_YUANTA_REALTIME = "AGGREGATED_FROM_YUANTA_REALTIME"
    """The only source this system ever writes: a bar this software aggregated itself
    from real-time Yuanta quote pushes it actually received while running. Never a
    vendor-supplied historical bar, third-party data, or synthesized/backfilled value —
    see the module docstring."""


@dataclass(frozen=True, slots=True)
class BarRecord:
    """One persisted row: a closed, validated `Bar` plus the metadata the implementation
    prompt requires storing alongside it."""

    bar: Bar
    period: BarPeriod
    trading_day: date
    """The trading day this bar's session belongs to — already resolved for the night
    session's midnight crossing (see `TradingCalendar.session_context_for`), never a bare
    calendar-date read off `bar.start`."""
    session: MarketSession
    source: BarDataSource
    is_gap_recovery: bool
    """True when this bar is the first to close after a `MarketDataGapDetected` window
    (e.g. immediately following a startup or reconnect) — the "完整性／gap" flag the
    implementation prompt requires per record. Does not itself imply this bar's own OHLCV
    is wrong; it flags that the *preceding* period may be incomplete."""
    created_at: Timestamp
    updated_at: Timestamp
    revision: int = 1
    """Bumped only by an explicit correction (see `application.ports.
    bar_record_repository.BarRecordRepository.apply_correction`) — never by an ordinary
    duplicate `BarClosed` replay, which is idempotently ignored instead."""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise InvalidBarRecordError(f"revision must be >= 1, got {self.revision}")
        if self.updated_at.value < self.created_at.value:
            raise InvalidBarRecordError("updated_at must be >= created_at")

    @property
    def identity(self) -> tuple[Instrument, ContractMonth, BarPeriod, Timestamp]:
        """商品、契約、週期及 start — the unique bar identity dedup and the database
        unique constraint are both built on."""
        return (self.bar.instrument, self.bar.contract, self.period, self.bar.start)


def rolling_two_month_start(today: date) -> date:
    """The inclusive start of the rolling "past two calendar months" window, per the
    implementation prompt's own worked example: 8/18's window starts 6/18. Not a fixed
    60-day window — genuinely two *calendar* months back, with the day-of-month clamped
    down when the target month is shorter (e.g. 8/31 -> 6/30, since June has no 31st)."""
    total_months = today.year * 12 + (today.month - 1) - 2
    year, month0 = divmod(total_months, 12)
    month = month0 + 1
    last_day_of_month = _calendar_module.monthrange(year, month)[1]
    day = min(today.day, last_day_of_month)
    return date(year, month, day)


@dataclass(frozen=True, slots=True)
class ContinuitySegment:
    """A maximal run of consecutively-adjacent closed bars — each bar's `start` exactly
    equals the previous bar's `end`, so no bar boundary is missing anywhere in the run.
    Only the segment reaching up to the most recent bar is safe to drive live signals
    from; see `continuous_segments`."""

    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        if not self.bars:
            raise InvalidBarRecordError("ContinuitySegment must contain at least one bar")

    @property
    def start(self) -> Timestamp:
        return self.bars[0].start

    @property
    def end(self) -> Timestamp:
        return self.bars[-1].end


def continuous_segments(records: Sequence[BarRecord]) -> list[ContinuitySegment]:
    """Splits `records` (must already be sorted ascending by `bar.start`, deduped by
    identity) into maximal continuity runs. Any missing expected boundary between two
    known bars — a genuine data gap, a session change, or a day the software wasn't
    running/subscribed — breaks the chain and starts a new segment; nothing here ever
    infers or fabricates a bar to bridge a gap."""
    if not records:
        return []
    segments: list[ContinuitySegment] = []
    current: list[Bar] = [records[0].bar]
    for prev, cur in zip(records, records[1:], strict=False):
        if prev.bar.end.value == cur.bar.start.value:
            current.append(cur.bar)
        else:
            segments.append(ContinuitySegment(bars=tuple(current)))
            current = [cur.bar]
    segments.append(ContinuitySegment(bars=tuple(current)))
    return segments
