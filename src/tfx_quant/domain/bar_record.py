"""BarRecord — the persisted-history wrapper around a closed `Bar`.

`Bar` (see `domain/bar.py`) stays the pure aggregation output used across the whole
codebase (events, streak counting, UI). `BarRecord` is a separate value object used only
by the persistence path (this Feature 04 extension): it carries the extra fields the
implementation prompt requires storing — 週期 (period), 交易日 (trading_day), session,
資料來源 (source), and the created/updated audit timestamps — without forcing every
existing `Bar` construction site to know about them.

`source` is `BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME` for the vast majority of rows
— bars this software aggregated itself from real-time Yuanta pushes it actually received
(see `docs/adr/0006-market-data-and-bar-aggregation.md` decision 7). A second value,
`BACKFILLED_FROM_YFINANCE`, exists for rows filled from the third-party `yfinance`
package when the rolling two-month window has a canonical bar identity this process never
observed live — see `docs/adr/0007-two-month-bar-history-persistence.md`'s yfinance
extension decision. Unlike `AGGREGATED_FROM_YUANTA_REALTIME`, this source is explicitly
**not** an official Yuanta/TAIFEX record — a genuine third-party feed, never conflated
with the real-time-aggregated path, and never presented as one. Neither source is ever a
manual import, a carried-forward previous close, or a synthesized value — a row's
`source` must always say honestly which one produced it.
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
    """A bar this software aggregated itself from real-time Yuanta quote pushes it
    actually received while running. Never third-party data or a synthesized value —
    see the module docstring."""
    BACKFILLED_FROM_YFINANCE = "BACKFILLED_FROM_YFINANCE"
    """A bar filled from the third-party `yfinance` package
    (`application.ports.yahoo_history_query.YahooHistoryQueryPort`), used only to cover
    a canonical bar identity within the rolling two-month window this process never
    observed live. A genuine third-party (Yahoo Finance) record, never a
    synthesized/carried-forward value — but also never a Yuanta/TAIFEX official record,
    so it is always kept distinct from `AGGREGATED_FROM_YUANTA_REALTIME` in storage, the
    UI, and any future signal logic. See the module docstring."""


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


@dataclass(frozen=True, slots=True)
class BarConflictAudit:
    """One `CONFLICT_REJECTED` outcome from `BarRecordRepository.upsert_closed_bar()`,
    kept as its own audit row rather than discarded — the "保存衝突 audit 與兩方摘要"
    the yfinance-backfill extension of the implementation prompt requires. `existing` is
    the canonical bar this codebase already had (never overwritten); `incoming` is the
    rejected bar an attempted write proposed instead — always a `BACKFILLED_FROM_YFINANCE`
    row today, since a same-identity conflict between two `AGGREGATED_FROM_YUANTA_REALTIME`
    writes can't happen (an identical live aggregator producing two different OHLCV
    values for the same boundary would be an aggregation bug, not an expected outcome
    this audit trail is meant to model)."""

    existing: BarRecord
    incoming: BarRecord
    detected_at: Timestamp

    def __post_init__(self) -> None:
        if self.existing.identity != self.incoming.identity:
            raise InvalidBarRecordError(
                "BarConflictAudit requires existing/incoming to share the same bar identity"
            )


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
