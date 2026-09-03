"""SqliteOrderRepository — the real `OrderRepository` (SQLite-backed).

Follows `persistence.sqlite_bar_record_repository.SqliteBarRecordRepository`'s exact
conventions: prices/`Decimal` stored as exact-round-trip `TEXT` (never `REAL`),
timestamps as `.isoformat()` `TEXT`, a `threading.Lock` serializing every statement.

One row per order intent (unlike bar records, which are one row per closed bar and never
mutated after insert): `save_intent` inserts the row once; every later transition is an
`update_intent` `UPDATE` against the same `local_order_id`. Uses its **own** dedicated
`sqlite3.Connection`/file — never shares a connection with
`SqliteBarRecordRepository`, since two independently-locked repositories over one shared
`sqlite3.Connection` object would not actually mutually exclude each other (see
`docs/adr/0008-order-and-fill-state-machine.md`).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from tfx_quant.application.ports.order_repository import (
    OrderIntentSaveOutcome,
    OrderRepositoryError,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderIntent, OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_debug, log_error

_logger = get_logger(__name__)

_TERMINAL_STATUS_VALUES = (
    OrderStatus.FILLED.value,
    OrderStatus.CANCELLED.value,
    OrderStatus.REJECTED.value,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_intents (
    local_order_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL,
    account_branch_id TEXT NOT NULL,
    account_no TEXT NOT NULL,
    account_sub_account TEXT NOT NULL,
    instrument TEXT NOT NULL,
    contract_year INTEGER NOT NULL,
    contract_month INTEGER NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity_lots INTEGER NOT NULL,
    status TEXT NOT NULL,
    broker_order_no TEXT,
    filled_quantity INTEGER NOT NULL,
    avg_fill_price TEXT,
    last_applied_broker_seq_no INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_event_summary TEXT NOT NULL,
    reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_intents_status ON order_intents (status);
CREATE INDEX IF NOT EXISTS idx_order_intents_contract
    ON order_intents (account_branch_id, account_no, account_sub_account, instrument,
                       contract_year, contract_month);
CREATE TABLE IF NOT EXISTS order_outbox (
    outbox_id TEXT PRIMARY KEY,
    local_order_id TEXT NOT NULL UNIQUE REFERENCES order_intents(local_order_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    checkpoint TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SELECT_COLUMNS = (
    "local_order_id, client_order_id, idempotency_key, workflow_id, account_branch_id, "
    "account_no, account_sub_account, instrument, contract_year, contract_month, side, "
    "kind, quantity_lots, status, broker_order_no, filled_quantity, avg_fill_price, "
    "last_applied_broker_seq_no, created_at, updated_at, last_event_summary, reject_reason"
)


def _dt(value: str) -> Timestamp:
    return Timestamp(datetime.fromisoformat(value))


def _price_text(price: Price | None) -> str | None:
    return None if price is None else str(price.amount)


def _parse_price(raw: object) -> Price | None:
    return None if raw is None else Price(Decimal(str(raw)))


def _parse_optional_int(raw: object) -> int | None:
    return None if raw is None else int(raw)  # type: ignore[call-overload]


class SqliteOrderRepository:
    """Implements `application.ports.order_repository.OrderRepository`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- Writes -----------------------------------------------------------------------

    def save_intent(self, intent: OrderIntent) -> OrderIntentSaveOutcome:
        start = time.monotonic()
        try:
            with self._lock:
                self._conn.execute(
                    f"""
                    INSERT INTO order_intents ({_SELECT_COLUMNS})
                    VALUES ({", ".join("?" * 22)})
                    """,
                    _intent_to_params(intent),
                )
                self._conn.commit()
                outcome = OrderIntentSaveOutcome.INSERTED
        except sqlite3.IntegrityError:
            outcome = OrderIntentSaveOutcome.DUPLICATE_KEY
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "order_intent_save_failed",
                local_order_id=str(intent.local_order_id.value),
                idempotency_key=intent.idempotency_key,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise OrderRepositoryError(f"order intent write failed: {exc}") from exc
        log_debug(
            _logger,
            "order_intent_save_completed",
            local_order_id=str(intent.local_order_id.value),
            idempotency_key=intent.idempotency_key,
            outcome=outcome.value,
            duration_ms=(time.monotonic() - start) * 1000,
        )
        return outcome

    def update_intent(self, intent: OrderIntent) -> None:
        start = time.monotonic()
        try:
            with self._lock:
                cursor = self._conn.execute(
                    """
                    UPDATE order_intents SET
                        status = ?, broker_order_no = ?, filled_quantity = ?,
                        avg_fill_price = ?, last_applied_broker_seq_no = ?, updated_at = ?,
                        last_event_summary = ?, reject_reason = ?
                    WHERE local_order_id = ?
                    """,
                    (
                        intent.status.value,
                        intent.broker_order_no,
                        intent.filled_quantity,
                        _price_text(intent.avg_fill_price),
                        intent.last_applied_broker_seq_no,
                        intent.updated_at.value.isoformat(),
                        intent.last_event_summary,
                        intent.reject_reason,
                        str(intent.local_order_id.value),
                    ),
                )
                self._conn.commit()
                updated = cursor.rowcount
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "order_intent_update_failed",
                local_order_id=str(intent.local_order_id.value),
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise OrderRepositoryError(f"order intent update failed: {exc}") from exc
        if updated == 0:
            raise OrderRepositoryError(
                f"no existing order_intents row for local_order_id={intent.local_order_id.value}"
            )
        log_debug(
            _logger,
            "order_intent_update_completed",
            local_order_id=str(intent.local_order_id.value),
            status=intent.status.value,
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def stage_submission(self, intent: OrderIntent) -> None:
        """Atomically persist SUBMITTING plus an outbox CALL_PENDING checkpoint."""
        start = time.monotonic()
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                cursor = self._conn.execute(
                    "UPDATE order_intents SET status=?, updated_at=?, last_event_summary=? "
                    "WHERE local_order_id=?",
                    (
                        intent.status.value,
                        intent.updated_at.value.isoformat(),
                        intent.last_event_summary,
                        str(intent.local_order_id.value),
                    ),
                )
                if cursor.rowcount != 1:
                    raise OrderRepositoryError("cannot stage a missing order intent")
                self._conn.execute(
                    "INSERT INTO order_outbox VALUES (?, ?, ?, 'CALL_PENDING', ?)",
                    (
                        str(intent.local_order_id.value),
                        str(intent.local_order_id.value),
                        intent.idempotency_key,
                        intent.updated_at.value.isoformat(),
                    ),
                )
                self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        log_debug(
            _logger,
            "order_outbox_staged",
            outbox_id=str(intent.local_order_id.value),
            idempotency_key=intent.idempotency_key,
            checkpoint="CALL_PENDING",
            commit_result="committed",
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def mark_outbox_checkpoint(self, local_order_id: LocalOrderId, checkpoint: str) -> None:
        allowed = {"BROKER_CALL_STARTED", "BROKER_CALL_RETURNED", "BROKER_CALL_FAILED"}
        if checkpoint not in allowed:
            raise ValueError(f"invalid outbox checkpoint: {checkpoint}")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE order_outbox SET checkpoint=?, updated_at=? WHERE local_order_id=?",
                (checkpoint, datetime.now().astimezone().isoformat(), str(local_order_id.value)),
            )
            self._conn.commit()
        if cursor.rowcount != 1:
            raise OrderRepositoryError("cannot checkpoint a missing outbox record")

    def list_unresolved_outbox(self) -> Sequence[tuple[str, str, str]]:
        with self._lock:
            return self._conn.execute(
                "SELECT outbox_id, idempotency_key, checkpoint FROM order_outbox "
                "WHERE checkpoint != 'BROKER_CALL_RETURNED' ORDER BY updated_at"
            ).fetchall()

    # -- Reads ------------------------------------------------------------------------

    def find_by_local_order_id(self, local_order_id: LocalOrderId) -> OrderIntent | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM order_intents WHERE local_order_id = ?",
                (str(local_order_id.value),),
            ).fetchone()
        return None if row is None else _row_to_intent(row)

    def find_by_client_order_id(self, client_order_id: ClientOrderId) -> OrderIntent | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM order_intents WHERE client_order_id = ?",
                (str(client_order_id.value),),
            ).fetchone()
        return None if row is None else _row_to_intent(row)

    def find_by_idempotency_key(self, idempotency_key: str) -> OrderIntent | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM order_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else _row_to_intent(row)

    def find_active_for_contract(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> Sequence[OrderIntent]:
        placeholders = ", ".join("?" * len(_TERMINAL_STATUS_VALUES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM order_intents "
                "WHERE account_branch_id = ? AND account_no = ? AND account_sub_account = ? "
                "AND instrument = ? AND contract_year = ? AND contract_month = ? "
                f"AND status NOT IN ({placeholders}) ORDER BY created_at ASC",
                (
                    account.branch_id,
                    account.account_no,
                    account.sub_account,
                    instrument.value,
                    contract.year,
                    contract.month,
                    *_TERMINAL_STATUS_VALUES,
                ),
            ).fetchall()
        return [_row_to_intent(row) for row in rows]

    def list_active(self) -> Sequence[OrderIntent]:
        placeholders = ", ".join("?" * len(_TERMINAL_STATUS_VALUES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM order_intents "
                f"WHERE status NOT IN ({placeholders}) ORDER BY created_at ASC",
                _TERMINAL_STATUS_VALUES,
            ).fetchall()
        return [_row_to_intent(row) for row in rows]

    def list_all(self) -> Sequence[OrderIntent]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM order_intents ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        return [_row_to_intent(row) for row in rows]


def _intent_to_params(intent: OrderIntent) -> tuple[object, ...]:
    return (
        str(intent.local_order_id.value),
        str(intent.client_order_id.value),
        intent.idempotency_key,
        intent.workflow_id,
        intent.account.branch_id,
        intent.account.account_no,
        intent.account.sub_account,
        intent.instrument.value,
        intent.contract.year,
        intent.contract.month,
        intent.side.value,
        intent.kind.value,
        intent.quantity.lots,
        intent.status.value,
        intent.broker_order_no,
        intent.filled_quantity,
        _price_text(intent.avg_fill_price),
        intent.last_applied_broker_seq_no,
        intent.created_at.value.isoformat(),
        intent.updated_at.value.isoformat(),
        intent.last_event_summary,
        intent.reject_reason,
    )


def _row_to_intent(row: tuple[object, ...]) -> OrderIntent:
    (
        local_order_id_raw,
        client_order_id_raw,
        idempotency_key,
        workflow_id,
        branch_id,
        account_no,
        sub_account,
        instrument_raw,
        contract_year,
        contract_month,
        side_raw,
        kind_raw,
        quantity_lots,
        status_raw,
        broker_order_no,
        filled_quantity,
        avg_fill_price_raw,
        last_applied_broker_seq_no,
        created_at,
        updated_at,
        last_event_summary,
        reject_reason,
    ) = row
    assert isinstance(local_order_id_raw, str)
    assert isinstance(client_order_id_raw, str)
    assert isinstance(idempotency_key, str)
    assert isinstance(workflow_id, str)
    assert isinstance(branch_id, str)
    assert isinstance(account_no, str)
    assert isinstance(sub_account, str)
    assert isinstance(instrument_raw, str)
    assert isinstance(contract_year, int)
    assert isinstance(contract_month, int)
    assert isinstance(side_raw, str)
    assert isinstance(kind_raw, str)
    assert isinstance(quantity_lots, int)
    assert isinstance(status_raw, str)
    assert isinstance(filled_quantity, int)
    assert isinstance(created_at, str)
    assert isinstance(updated_at, str)
    assert isinstance(last_event_summary, str)
    return OrderIntent(
        local_order_id=LocalOrderId(UUID(local_order_id_raw)),
        client_order_id=ClientOrderId(UUID(client_order_id_raw)),
        workflow_id=workflow_id,
        idempotency_key=idempotency_key,
        account=TradingAccount(branch_id=branch_id, account_no=account_no, sub_account=sub_account),
        instrument=Instrument(instrument_raw),
        contract=ContractMonth(year=contract_year, month=contract_month),
        side=Side(side_raw),
        kind=OrderKind(kind_raw),
        quantity=Quantity(quantity_lots),
        status=OrderStatus(status_raw),
        broker_order_no=None if broker_order_no is None else str(broker_order_no),
        filled_quantity=filled_quantity,
        avg_fill_price=_parse_price(avg_fill_price_raw),
        last_applied_broker_seq_no=_parse_optional_int(last_applied_broker_seq_no),
        created_at=_dt(created_at),
        updated_at=_dt(updated_at),
        last_event_summary=last_event_summary,
        reject_reason=None if reject_reason is None else str(reject_reason),
    )


__all__ = ["SqliteOrderRepository"]
