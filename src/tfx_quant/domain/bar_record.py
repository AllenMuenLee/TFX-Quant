"""BarRecord — the persisted-history wrapper around a closed `Bar`.

`Bar` (see `domain/bar.py`) stays the pure aggregation output used across the whole
codebase (events, streak counting, UI). `BarRecord` is a separate value object used only
by the persistence path (this Feature 04 extension): it carries the extra fields the
implementation prompt requires storing — 週期 (period), 交易日 (trading_day), session,
資料來源 (source), and the created/updated audit timestamps — without forcing every
existing `Bar` construction site to know about them.

Every bar this codebase ever persists comes from the third-party `yfinance` package —
there is no Yuanta/TAIFEX official price feed in this system (see `implementation prompt/
04-market-data-and-60m-bars/implementation-prompt.md`'s banner). `source` distinguishes
*when* the write happened, not *where* the data came from: `POLLED_FROM_YFINANCE` is the
common case — `application.market_data.bar_service.MarketDataBarService` polling
yfinance frequently and writing a bar promptly after it closed. `BACKFILLED_FROM_YFINANCE`
is a later, coarser gap-fill sweep (`application.market_data.
bar_history_backfill_service.BarHistoryBackfillService`) covering a canonical bar
identity the frequent poll missed (process was down, a transient query failure, etc.) —
see `docs/adr/0007-two-month-bar-history-persistence.md`'s yfinance extension decision.
Neither source is ever a manual import, a carried-forward previous close, or a
synthesized value, and neither is ever presented as an official Yuanta/TAIFEX record — a
row's `source` must always say honestly which write path produced it.
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
    LOCAL_YUANTA_REALTIME = "LOCAL_YUANTA_REALTIME"
    POLLED_FROM_YFINANCE = "POLLED_FROM_YFINANCE"
    """A bar `MarketDataBarService` observed by polling `yfinance`
    (`application.ports.yahoo_history_query.YahooHistoryQueryPort`) shortly after it
    closed and wrote promptly. The common case for a healthy, continuously-running
    process. Never a synthesized/carried-forward value — see the module docstring."""
    BACKFILLED_FROM_YFINANCE = "BACKFILLED_FROM_YFINANCE"
    """A bar filled by `BarHistoryBackfillService`'s coarser gap-fill sweep, used to
    cover a canonical bar identity within the rolling two-month window the frequent poll
    never observed (process was down, a transient query failure, etc.). Both sources are
    genuine third-party (Yahoo Finance) records, never a Yuanta/TAIFEX official one — kept
    distinct from `POLLED_FROM_YFINANCE` in storage, the UI, and any future signal logic
    purely to say honestly which write path produced a given row. See the module
    docstring."""


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
    source_first_sequence: int | None = None
    source_last_sequence: int | None = None
    is_complete: bool = True
    """Bumped only by an explicit correction (see `application.ports.
    bar_record_repository.BarRecordRepository.apply_correction`) — never by an ordinary
    duplicate `BarClosed` replay, which is idempotently ignored instead."""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise InvalidBarRecordError(f"revision must be >= 1, got {self.revision}")
        if self.updated_at.value < self.created_at.value:
            raise InvalidBarRecordError("updated_at must be >= created_at")
        if (self.source_first_sequence is None) != (self.source_last_sequence is None):
            raise InvalidBarRecordError("source event sequence range must have both endpoints")
        if (
            self.source_first_sequence is not None
            and self.source_last_sequence is not None
            and self.source_first_sequence > self.source_last_sequence
        ):
            raise InvalidBarRecordError("source event sequence range is reversed")

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
    rejected bar an attempted write proposed instead. Most commonly `incoming` is a
    `BACKFILLED_FROM_YFINANCE` row conflicting with an already-`POLLED_FROM_YFINANCE`
    `existing` row — `yfinance` itself revising a bar's OHLCV between the fast poll and a
    later gap-fill sweep — but a `POLLED_FROM_YFINANCE` vs `POLLED_FROM_YFINANCE` conflict
    is also possible if yfinance revises a bar between two successive polls; this audit
    trail doesn't assume either direction."""

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
