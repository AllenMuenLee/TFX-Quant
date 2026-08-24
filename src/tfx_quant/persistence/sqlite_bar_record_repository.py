"""SqliteBarRecordRepository — the real `BarRecordRepository` (SQLite-backed).

Every price is stored as `TEXT` (the exact `str(Decimal(...))`), never a SQLite
`REAL`/float column — this codebase never lets a price pass through a float
representation (see `domain/money.py`). Every timestamp is stored as its Asia/Taipei
`datetime.isoformat()` string (always carries the `+08:00` offset — see
`domain/timestamp.py`'s `Timestamp` invariant), which round-trips unambiguously through
`datetime.fromisoformat()` without needing a separate stored time zone column.

Thread-safety: this repository is written to from a dedicated background writer thread
(`MarketDataBarService`'s bounded write queue) while also being queried from the wx UI
thread and the `EventCoordinator` consumer thread. A single `threading.Lock` serializes
every statement against the one `sqlite3.Connection` — the connection itself must be
opened with `check_same_thread=False` (see `persistence/sqlite_connection.py`) for this
to be legal at all.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from tfx_quant.application.ports.bar_record_repository import (
    BarRecordNotFoundError,
    BarUpsertOutcome,
    BarUpsertRepositoryError,
    RetentionCleanupSummary,
)
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import (
    BarConflictAudit,
    BarDataSource,
    BarPeriod,
    BarRecord,
    MarketSession,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_debug, log_error, log_info

_logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_records (
    instrument TEXT NOT NULL,
    contract_year INTEGER NOT NULL,
    contract_month INTEGER NOT NULL,
    period TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    session TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    is_gap_recovery INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_first_sequence INTEGER,
    source_last_sequence INTEGER,
    is_complete INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (instrument, contract_year, contract_month, period, start_at)
);
CREATE INDEX IF NOT EXISTS idx_bar_records_trading_day ON bar_records (trading_day);
CREATE TABLE IF NOT EXISTS bar_record_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    contract_year INTEGER NOT NULL,
    contract_month INTEGER NOT NULL,
    period TEXT NOT NULL,
    start_at TEXT NOT NULL,
    previous_open TEXT NOT NULL,
    previous_high TEXT NOT NULL,
    previous_low TEXT NOT NULL,
    previous_close TEXT NOT NULL,
    previous_volume INTEGER NOT NULL,
    new_open TEXT NOT NULL,
    new_high TEXT NOT NULL,
    new_low TEXT NOT NULL,
    new_close TEXT NOT NULL,
    new_volume INTEGER NOT NULL,
    reason TEXT NOT NULL,
    revised_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bar_backfill_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    contract_year INTEGER NOT NULL,
    contract_month INTEGER NOT NULL,
    period TEXT NOT NULL,
    start_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    existing_source TEXT NOT NULL,
    existing_open TEXT NOT NULL,
    existing_high TEXT NOT NULL,
    existing_low TEXT NOT NULL,
    existing_close TEXT NOT NULL,
    existing_volume INTEGER NOT NULL,
    incoming_source TEXT NOT NULL,
    incoming_open TEXT NOT NULL,
    incoming_high TEXT NOT NULL,
    incoming_low TEXT NOT NULL,
    incoming_close TEXT NOT NULL,
    incoming_volume INTEGER NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bar_backfill_conflicts_trading_day
    ON bar_backfill_conflicts (instrument, contract_year, contract_month, period, trading_day);
"""

_SELECT_COLUMNS = (
    "instrument, contract_year, contract_month, period, start_at, end_at, trading_day, "
    "session, open, high, low, close, volume, source, is_gap_recovery, revision, "
    "created_at, updated_at, source_first_sequence, source_last_sequence, is_complete"
)


def _dt(value: str) -> Timestamp:
    return Timestamp(datetime.fromisoformat(value))


class SqliteBarRecordRepository:
    """Implements `application.ports.bar_record_repository.BarRecordRepository`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_source_metadata()
            self._conn.commit()

    def _migrate_source_metadata(self) -> None:
        """Add Feature 04 provenance columns to databases created by older builds."""
        columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(bar_records)").fetchall()
        }
        additions = {
            "source_first_sequence": "INTEGER",
            "source_last_sequence": "INTEGER",
            "is_complete": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE bar_records ADD COLUMN {name} {declaration}")

    # -- Writes -----------------------------------------------------------------

    def upsert_closed_bar(self, record: BarRecord) -> BarUpsertOutcome:
        start = time.monotonic()
        identity = (
            record.bar.instrument.value,
            record.bar.contract.code,
            record.bar.start.value.isoformat(),
        )
        try:
            with self._lock:
                existing = self._select_one(record)
                if existing is None:
                    self._insert(record)
                    self._conn.commit()
                    outcome = BarUpsertOutcome.INSERTED
                elif _ohlcv_matches(existing.bar, record.bar):
                    outcome = BarUpsertOutcome.DUPLICATE_IGNORED
                else:
                    outcome = BarUpsertOutcome.CONFLICT_REJECTED
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "bar_record_upsert_failed",
                instrument=identity[0],
                contract=identity[1],
                bar_start=identity[2],
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise BarUpsertRepositoryError(f"bar record write failed: {exc}") from exc
        log_debug(
            _logger,
            "bar_record_upsert_completed",
            instrument=identity[0],
            contract=identity[1],
            bar_start=identity[2],
            outcome=outcome.value,
            duration_ms=(time.monotonic() - start) * 1000,
        )
        return outcome

    def apply_correction(self, record: BarRecord, *, reason: str) -> None:
        try:
            with self._lock:
                existing = self._select_one(record)
                if existing is None:
                    log_error(
                        _logger,
                        "bar_record_correction_no_existing_row",
                        instrument=record.bar.instrument.value,
                        contract=record.bar.contract.code,
                        bar_start=record.bar.start.value.isoformat(),
                    )
                    raise BarRecordNotFoundError(
                        f"no existing bar record for identity {record.identity!r} to correct"
                    )
                self._conn.execute(
                    """
                    INSERT INTO bar_record_revisions (
                        instrument, contract_year, contract_month, period, start_at,
                        previous_open, previous_high, previous_low, previous_close,
                        previous_volume, new_open, new_high, new_low, new_close, new_volume,
                        reason, revised_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.bar.instrument.value,
                        record.bar.contract.year,
                        record.bar.contract.month,
                        record.period.value,
                        record.bar.start.value.isoformat(),
                        str(existing.bar.open.amount),
                        str(existing.bar.high.amount),
                        str(existing.bar.low.amount),
                        str(existing.bar.close.amount),
                        existing.bar.volume,
                        str(record.bar.open.amount),
                        str(record.bar.high.amount),
                        str(record.bar.low.amount),
                        str(record.bar.close.amount),
                        record.bar.volume,
                        reason,
                        record.updated_at.value.isoformat(),
                    ),
                )
                self._update(record, revision=existing.revision + 1)
                self._conn.commit()
        except sqlite3.Error as exc:
            raise BarUpsertRepositoryError(f"bar record correction failed: {exc}") from exc
        log_info(
            _logger,
            "bar_record_correction_applied",
            instrument=record.bar.instrument.value,
            contract=record.bar.contract.code,
            bar_start=record.bar.start.value.isoformat(),
            revision=existing.revision + 1,
            reason=reason,
        )

    def _select_one(self, record: BarRecord) -> BarRecord | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM bar_records "
            "WHERE instrument = ? AND contract_year = ? AND contract_month = ? "
            "AND period = ? AND start_at = ?",
            (
                record.bar.instrument.value,
                record.bar.contract.year,
                record.bar.contract.month,
                record.period.value,
                record.bar.start.value.isoformat(),
            ),
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def _insert(self, record: BarRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO bar_records (
                instrument, contract_year, contract_month, period, start_at, end_at,
                trading_day, session, open, high, low, close, volume, source,
                is_gap_recovery, revision, created_at, updated_at,
                source_first_sequence, source_last_sequence, is_complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _record_to_params(record),
        )

    def _update(self, record: BarRecord, *, revision: int) -> None:
        self._conn.execute(
            """
            UPDATE bar_records SET
                end_at = ?, trading_day = ?, session = ?, open = ?, high = ?, low = ?,
                close = ?, volume = ?, source = ?, is_gap_recovery = ?, revision = ?,
                updated_at = ?, source_first_sequence = ?, source_last_sequence = ?,
                is_complete = ?
            WHERE instrument = ? AND contract_year = ? AND contract_month = ?
                AND period = ? AND start_at = ?
            """,
            (
                record.bar.end.value.isoformat(),
                record.trading_day.isoformat(),
                record.session.value,
                str(record.bar.open.amount),
                str(record.bar.high.amount),
                str(record.bar.low.amount),
                str(record.bar.close.amount),
                record.bar.volume,
                record.source.value,
                int(record.is_gap_recovery),
                revision,
                record.updated_at.value.isoformat(),
                record.source_first_sequence,
                record.source_last_sequence,
                int(record.is_complete),
                record.bar.instrument.value,
                record.bar.contract.year,
                record.bar.contract.month,
                record.period.value,
                record.bar.start.value.isoformat(),
            ),
        )

    # -- Reads --------------------------------------------------------------------

    def get_one(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod, start: Timestamp
    ) -> BarRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM bar_records "
                "WHERE instrument = ? AND contract_year = ? AND contract_month = ? "
                "AND period = ? AND start_at = ?",
                (
                    instrument.value,
                    contract.year,
                    contract.month,
                    period.value,
                    start.value.isoformat(),
                ),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_recent(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        since_trading_day: date,
    ) -> Sequence[BarRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM bar_records "
                "WHERE instrument = ? AND contract_year = ? AND contract_month = ? "
                "AND period = ? AND trading_day >= ? ORDER BY start_at ASC",
                (
                    instrument.value,
                    contract.year,
                    contract.month,
                    period.value,
                    since_trading_day.isoformat(),
                ),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def query_range(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        start_date: date,
        end_date: date,
    ) -> Sequence[BarRecord]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM bar_records "
                "WHERE instrument = ? AND contract_year = ? AND contract_month = ? "
                "AND period = ? AND trading_day BETWEEN ? AND ? ORDER BY start_at ASC",
                (
                    instrument.value,
                    contract.year,
                    contract.month,
                    period.value,
                    start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def earliest_recorded_at(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod
    ) -> Timestamp | None:
        return self._min_or_max_start(instrument, contract, period, func="MIN")

    def latest_recorded_at(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod
    ) -> Timestamp | None:
        return self._min_or_max_start(instrument, contract, period, func="MAX")

    def _min_or_max_start(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod, *, func: str
    ) -> Timestamp | None:
        assert func in ("MIN", "MAX")
        with self._lock:
            row = self._conn.execute(
                f"SELECT {func}(start_at) FROM bar_records "
                "WHERE instrument = ? AND contract_year = ? AND contract_month = ? "
                "AND period = ?",
                (instrument.value, contract.year, contract.month, period.value),
            ).fetchone()
        value = row[0] if row is not None else None
        return None if value is None else _dt(value)

    def delete_before(
        self, cutoff_trading_day: date, *, ran_at: Timestamp
    ) -> RetentionCleanupSummary:
        start = time.monotonic()
        audit_id = uuid4().hex
        try:
            with self._lock:
                candidate_row = self._conn.execute(
                    "SELECT COUNT(*) FROM bar_records WHERE trading_day < ?",
                    (cutoff_trading_day.isoformat(),),
                ).fetchone()
                candidate_count = 0 if candidate_row is None else int(candidate_row[0])
                cursor = self._conn.execute(
                    "DELETE FROM bar_records WHERE trading_day < ?",
                    (cutoff_trading_day.isoformat(),),
                )
                self._conn.commit()
                deleted_count = cursor.rowcount
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "bar_record_retention_delete_failed",
                cutoff_trading_day=cutoff_trading_day.isoformat(),
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise BarUpsertRepositoryError(f"retention cleanup failed: {exc}") from exc
        log_debug(
            _logger,
            "bar_record_retention_delete_completed",
            cutoff_trading_day=cutoff_trading_day.isoformat(),
            candidate_count=candidate_count,
            deleted_count=deleted_count,
            audit_id=audit_id,
            duration_ms=(time.monotonic() - start) * 1000,
            transaction_committed=True,
        )
        return RetentionCleanupSummary(
            cutoff_trading_day=cutoff_trading_day,
            candidate_count=candidate_count,
            deleted_count=deleted_count,
            ran_at=ran_at,
            audit_id=audit_id,
        )

    def record_conflict(self, audit: BarConflictAudit) -> None:
        existing, incoming = audit.existing, audit.incoming
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO bar_backfill_conflicts (
                        instrument, contract_year, contract_month, period, start_at,
                        trading_day, existing_source, existing_open, existing_high,
                        existing_low, existing_close, existing_volume, incoming_source,
                        incoming_open, incoming_high, incoming_low, incoming_close,
                        incoming_volume, detected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incoming.bar.instrument.value,
                        incoming.bar.contract.year,
                        incoming.bar.contract.month,
                        incoming.period.value,
                        incoming.bar.start.value.isoformat(),
                        incoming.trading_day.isoformat(),
                        existing.source.value,
                        str(existing.bar.open.amount),
                        str(existing.bar.high.amount),
                        str(existing.bar.low.amount),
                        str(existing.bar.close.amount),
                        existing.bar.volume,
                        incoming.source.value,
                        str(incoming.bar.open.amount),
                        str(incoming.bar.high.amount),
                        str(incoming.bar.low.amount),
                        str(incoming.bar.close.amount),
                        incoming.bar.volume,
                        audit.detected_at.value.isoformat(),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            raise BarUpsertRepositoryError(f"bar conflict audit write failed: {exc}") from exc
        log_error(
            _logger,
            "bar_backfill_conflict_recorded",
            instrument=incoming.bar.instrument.value,
            contract=incoming.bar.contract.code,
            bar_start=incoming.bar.start.value.isoformat(),
            existing_source=existing.source.value,
            incoming_source=incoming.source.value,
        )

    def list_conflicted_trading_days(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        since_trading_day: date,
    ) -> frozenset[date]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT trading_day FROM bar_backfill_conflicts "
                "WHERE instrument = ? AND contract_year = ? AND contract_month = ? "
                "AND period = ? AND trading_day >= ?",
                (
                    instrument.value,
                    contract.year,
                    contract.month,
                    period.value,
                    since_trading_day.isoformat(),
                ),
            ).fetchall()
        return frozenset(date.fromisoformat(row[0]) for row in rows)


def _ohlcv_matches(a: Bar, b: Bar) -> bool:
    return (
        a.open.amount == b.open.amount
        and a.high.amount == b.high.amount
        and a.low.amount == b.low.amount
        and a.close.amount == b.close.amount
        and a.volume == b.volume
        and a.end.value == b.end.value
    )


def _record_to_params(record: BarRecord) -> tuple[object, ...]:
    return (
        record.bar.instrument.value,
        record.bar.contract.year,
        record.bar.contract.month,
        record.period.value,
        record.bar.start.value.isoformat(),
        record.bar.end.value.isoformat(),
        record.trading_day.isoformat(),
        record.session.value,
        str(record.bar.open.amount),
        str(record.bar.high.amount),
        str(record.bar.low.amount),
        str(record.bar.close.amount),
        record.bar.volume,
        record.source.value,
        int(record.is_gap_recovery),
        record.revision,
        record.created_at.value.isoformat(),
        record.updated_at.value.isoformat(),
        record.source_first_sequence,
        record.source_last_sequence,
        int(record.is_complete),
    )


def _row_to_record(row: tuple[object, ...]) -> BarRecord:
    (
        instrument_raw,
        contract_year,
        contract_month,
        period_raw,
        start_at,
        end_at,
        trading_day_raw,
        session_raw,
        open_raw,
        high_raw,
        low_raw,
        close_raw,
        volume,
        source_raw,
        is_gap_recovery,
        revision,
        created_at,
        updated_at,
        source_first_sequence,
        source_last_sequence,
        is_complete,
    ) = row
    assert isinstance(instrument_raw, str)
    assert isinstance(contract_year, int)
    assert isinstance(contract_month, int)
    assert isinstance(period_raw, str)
    assert isinstance(start_at, str)
    assert isinstance(end_at, str)
    assert isinstance(trading_day_raw, str)
    assert isinstance(session_raw, str)
    assert isinstance(volume, int)
    assert isinstance(source_raw, str)
    assert isinstance(is_gap_recovery, int)
    assert isinstance(revision, int)
    assert isinstance(created_at, str)
    assert isinstance(updated_at, str)
    bar = Bar(
        instrument=Instrument(instrument_raw),
        contract=ContractMonth(year=contract_year, month=contract_month),
        open=Price(Decimal(str(open_raw))),
        high=Price(Decimal(str(high_raw))),
        low=Price(Decimal(str(low_raw))),
        close=Price(Decimal(str(close_raw))),
        volume=volume,
        start=_dt(start_at),
        end=_dt(end_at),
    )
    return BarRecord(
        bar=bar,
        period=BarPeriod(period_raw),
        trading_day=date.fromisoformat(trading_day_raw),
        session=MarketSession(session_raw),
        source=BarDataSource(source_raw),
        is_gap_recovery=bool(is_gap_recovery),
        created_at=_dt(created_at),
        updated_at=_dt(updated_at),
        revision=revision,
        source_first_sequence=(
            None if source_first_sequence is None else int(str(source_first_sequence))
        ),
        source_last_sequence=(
            None if source_last_sequence is None else int(str(source_last_sequence))
        ),
        is_complete=bool(is_complete),
    )


__all__ = ["SqliteBarRecordRepository"]
