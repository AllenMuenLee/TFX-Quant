"""BarRecordRepository — the controlled persistence port for closed-bar history.

`persistence.sqlite_bar_record_repository.SqliteBarRecordRepository` is the real
implementation (SQLite, per this codebase's existing `persistence.sqlite_connection`
factory). Only closed, validated bars are ever written here — see
`application.market_data.bar_service.MarketDataBarService`, the only caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Protocol

from tfx_quant.domain.bar_record import BarPeriod, BarRecord
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.timestamp import Timestamp


class BarUpsertOutcome(StrEnum):
    INSERTED = "INSERTED"
    """No row existed for this identity — inserted as a new closed bar."""
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    """A row already existed for this identity with identical OHLCV — a duplicate
    `BarClosed` replay (repeated event, process-restart re-delivery, etc.), safely
    ignored rather than re-written."""
    CONFLICT_REJECTED = "CONFLICT_REJECTED"
    """A row already existed for this identity with *different* OHLCV. Never silently
    overwritten — a late tick or a re-aggregation bug must not amend an already-saved
    closed bar. Only `apply_correction()` may change it, and only with an explicit
    reason and an audit trail."""


class BarUpsertRepositoryError(Exception):
    """Raised by a `BarRecordRepository` implementation when a write could not be
    completed (I/O failure, locked database, etc.) — distinct from `CONFLICT_REJECTED`,
    which is an ordinary, expected outcome, not a failure. The caller (`MarketDataBarService`'s
    bounded-retry writer) is expected to catch this and retry/alert rather than let it
    propagate into the event-dispatch thread."""


class BarRecordNotFoundError(Exception):
    """Raised by `apply_correction()` when no existing row matches the record's
    identity — a correction can only revise an already-persisted bar."""


class RetentionCleanupSummary:
    """Result of one `delete_before()` call — the "audit 摘要" the implementation
    prompt requires retention cleanup to produce."""

    __slots__ = ("cutoff_trading_day", "deleted_count", "ran_at")

    def __init__(self, *, cutoff_trading_day: date, deleted_count: int, ran_at: Timestamp) -> None:
        self.cutoff_trading_day = cutoff_trading_day
        self.deleted_count = deleted_count
        self.ran_at = ran_at

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RetentionCleanupSummary):
            return NotImplemented
        return (
            self.cutoff_trading_day == other.cutoff_trading_day
            and self.deleted_count == other.deleted_count
            and self.ran_at == other.ran_at
        )

    def __repr__(self) -> str:
        return (
            f"RetentionCleanupSummary(cutoff_trading_day={self.cutoff_trading_day!r}, "
            f"deleted_count={self.deleted_count!r}, ran_at={self.ran_at!r})"
        )


class BarRecordRepository(Protocol):
    def upsert_closed_bar(self, record: BarRecord) -> BarUpsertOutcome:
        """Idempotent write of one already-closed, validated bar. Raises
        `BarUpsertRepositoryError` if the write itself failed (I/O), never for an
        ordinary duplicate or conflict — those are outcomes, not errors."""
        ...

    def apply_correction(self, record: BarRecord, *, reason: str) -> None:
        """Explicitly revises an existing row (bumps `revision`, records
        `record.updated_at`, and writes an audit entry with `reason`) — the only path
        by which an already-persisted closed bar's OHLCV may ever change. Raises
        `BarRecordNotFoundError` if no row exists for `record.identity`."""
        ...

    def list_recent(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        since_trading_day: date,
    ) -> Sequence[BarRecord]:
        """Every record for `(instrument, contract, period)` with `trading_day >=
        since_trading_day`, ascending by `bar.start`, deduped by identity (the
        repository's own unique constraint already guarantees this)."""
        ...

    def query_range(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        start_date: date,
        end_date: date,
    ) -> Sequence[BarRecord]:
        """Every record with `start_date <= trading_day <= end_date`, ascending by
        `bar.start` — the UI browse-by-date-range query."""
        ...

    def earliest_recorded_at(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod
    ) -> Timestamp | None:
        """`None` means nothing has ever been recorded for this
        (instrument, contract, period) — the normal, expected state on first
        activation."""
        ...

    def latest_recorded_at(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod
    ) -> Timestamp | None: ...

    def delete_before(
        self, cutoff_trading_day: date, *, ran_at: Timestamp
    ) -> RetentionCleanupSummary:
        """Deletes every bar record (any instrument/contract) with
        `trading_day < cutoff_trading_day`. Scoped to the bar-records table only — never
        touches orders, fills, positions, or audit data (which don't live in this
        repository at all)."""
        ...
