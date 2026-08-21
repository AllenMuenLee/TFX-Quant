"""SqliteReversalWorkflowRepository — the real `ReversalWorkflowRepository` (SQLite-
backed).

Follows `persistence.sqlite_order_repository.SqliteOrderRepository`'s exact
conventions: one row per workflow, mutated via `UPDATE`; `threading.Lock` serializing
every statement; its **own** dedicated `sqlite3.Connection`/file — never shares
`orders.sqlite3`'s or `market_data.sqlite3`'s connection (two independently-locked
repositories over one shared connection would not actually mutually exclude each other
— see `docs/adr/0008-order-and-fill-state-machine.md` decision 7, and
`docs/adr/0009-safe-reversal-and-scaling.md`).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from tfx_quant.application.ports.reversal_workflow_repository import (
    ReversalWorkflowRepositoryError,
    ReversalWorkflowSaveOutcome,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reversal_workflow import (
    ReversalWorkflowId,
    ReversalWorkflowRecord,
    ReversalWorkflowState,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_debug, log_error

_logger = get_logger(__name__)

_INACTIVE_STATE_VALUES = (
    ReversalWorkflowState.COMPLETED.value,
    ReversalWorkflowState.BLOCKED.value,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reversal_workflows (
    workflow_id TEXT PRIMARY KEY,
    trigger_key TEXT NOT NULL UNIQUE,
    account_branch_id TEXT NOT NULL,
    account_no TEXT NOT NULL,
    account_sub_account TEXT NOT NULL,
    instrument TEXT NOT NULL,
    contract_year INTEGER NOT NULL,
    contract_month INTEGER NOT NULL,
    state TEXT NOT NULL,
    price TEXT,
    starting_net_lots INTEGER,
    reversal_side TEXT,
    close_client_order_id TEXT,
    entry_client_order_id TEXT,
    pause_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reversal_workflows_state ON reversal_workflows (state);
CREATE INDEX IF NOT EXISTS idx_reversal_workflows_contract
    ON reversal_workflows (account_branch_id, account_no, account_sub_account, instrument,
                            contract_year, contract_month);
"""

_SELECT_COLUMNS = (
    "workflow_id, trigger_key, account_branch_id, account_no, account_sub_account, "
    "instrument, contract_year, contract_month, state, price, starting_net_lots, "
    "reversal_side, close_client_order_id, entry_client_order_id, pause_reason, "
    "created_at, updated_at"
)


def _dt(value: str) -> Timestamp:
    return Timestamp(datetime.fromisoformat(value))


class SqliteReversalWorkflowRepository:
    """Implements `application.ports.reversal_workflow_repository.
    ReversalWorkflowRepository`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- Writes -----------------------------------------------------------------------

    def save(self, record: ReversalWorkflowRecord) -> ReversalWorkflowSaveOutcome:
        start = time.monotonic()
        try:
            with self._lock:
                self._conn.execute(
                    f"""
                    INSERT INTO reversal_workflows ({_SELECT_COLUMNS})
                    VALUES ({", ".join("?" * 17)})
                    """,
                    _record_to_params(record),
                )
                self._conn.commit()
                outcome = ReversalWorkflowSaveOutcome.INSERTED
        except sqlite3.IntegrityError:
            outcome = ReversalWorkflowSaveOutcome.DUPLICATE_KEY
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "reversal_workflow_save_failed",
                workflow_id=str(record.workflow_id.value),
                trigger_key=record.trigger_key,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise ReversalWorkflowRepositoryError(f"reversal workflow write failed: {exc}") from exc
        log_debug(
            _logger,
            "reversal_workflow_save_completed",
            workflow_id=str(record.workflow_id.value),
            trigger_key=record.trigger_key,
            outcome=outcome.value,
            duration_ms=(time.monotonic() - start) * 1000,
        )
        return outcome

    def update(self, record: ReversalWorkflowRecord) -> None:
        start = time.monotonic()
        try:
            with self._lock:
                cursor = self._conn.execute(
                    """
                    UPDATE reversal_workflows SET
                        state = ?, starting_net_lots = ?, reversal_side = ?,
                        close_client_order_id = ?, entry_client_order_id = ?,
                        pause_reason = ?, updated_at = ?
                    WHERE workflow_id = ?
                    """,
                    (
                        record.state.value,
                        None if record.starting_net is None else record.starting_net.lots,
                        None if record.reversal_side is None else record.reversal_side.value,
                        _client_order_id_text(record.close_client_order_id),
                        _client_order_id_text(record.entry_client_order_id),
                        record.pause_reason,
                        record.updated_at.value.isoformat(),
                        str(record.workflow_id.value),
                    ),
                )
                self._conn.commit()
                updated = cursor.rowcount
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "reversal_workflow_update_failed",
                workflow_id=str(record.workflow_id.value),
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise ReversalWorkflowRepositoryError(
                f"reversal workflow update failed: {exc}"
            ) from exc
        if updated == 0:
            raise ReversalWorkflowRepositoryError(
                f"no existing reversal_workflows row for workflow_id={record.workflow_id.value}"
            )
        log_debug(
            _logger,
            "reversal_workflow_update_completed",
            workflow_id=str(record.workflow_id.value),
            state=record.state.value,
            duration_ms=(time.monotonic() - start) * 1000,
        )

    # -- Reads ------------------------------------------------------------------------

    def find_by_workflow_id(self, workflow_id: ReversalWorkflowId) -> ReversalWorkflowRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM reversal_workflows WHERE workflow_id = ?",
                (str(workflow_id.value),),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def find_by_trigger_key(self, trigger_key: str) -> ReversalWorkflowRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM reversal_workflows WHERE trigger_key = ?",
                (trigger_key,),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def find_active_for_contract(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> Sequence[ReversalWorkflowRecord]:
        placeholders = ", ".join("?" * len(_INACTIVE_STATE_VALUES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM reversal_workflows "
                "WHERE account_branch_id = ? AND account_no = ? AND account_sub_account = ? "
                "AND instrument = ? AND contract_year = ? AND contract_month = ? "
                f"AND state NOT IN ({placeholders}) ORDER BY created_at ASC",
                (
                    account.branch_id,
                    account.account_no,
                    account.sub_account,
                    instrument.value,
                    contract.year,
                    contract.month,
                    *_INACTIVE_STATE_VALUES,
                ),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def list_active(self) -> Sequence[ReversalWorkflowRecord]:
        placeholders = ", ".join("?" * len(_INACTIVE_STATE_VALUES))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM reversal_workflows "
                f"WHERE state NOT IN ({placeholders}) ORDER BY created_at ASC",
                _INACTIVE_STATE_VALUES,
            ).fetchall()
        return [_row_to_record(row) for row in rows]


def _client_order_id_text(value: ClientOrderId | None) -> str | None:
    return None if value is None else str(value.value)


def _parse_client_order_id(raw: object) -> ClientOrderId | None:
    return None if raw is None else ClientOrderId(UUID(str(raw)))


def _record_to_params(record: ReversalWorkflowRecord) -> tuple[object, ...]:
    return (
        str(record.workflow_id.value),
        record.trigger_key,
        record.account.branch_id,
        record.account.account_no,
        record.account.sub_account,
        record.instrument.value,
        record.contract.year,
        record.contract.month,
        record.state.value,
        None if record.price is None else str(record.price.amount),
        None if record.starting_net is None else record.starting_net.lots,
        None if record.reversal_side is None else record.reversal_side.value,
        _client_order_id_text(record.close_client_order_id),
        _client_order_id_text(record.entry_client_order_id),
        record.pause_reason,
        record.created_at.value.isoformat(),
        record.updated_at.value.isoformat(),
    )


def _row_to_record(row: tuple[object, ...]) -> ReversalWorkflowRecord:
    (
        workflow_id_raw,
        trigger_key,
        branch_id,
        account_no,
        sub_account,
        instrument_raw,
        contract_year,
        contract_month,
        state_raw,
        price_raw,
        starting_net_lots,
        reversal_side_raw,
        close_client_order_id_raw,
        entry_client_order_id_raw,
        pause_reason,
        created_at,
        updated_at,
    ) = row
    assert isinstance(workflow_id_raw, str)
    assert isinstance(trigger_key, str)
    assert isinstance(branch_id, str)
    assert isinstance(account_no, str)
    assert isinstance(sub_account, str)
    assert isinstance(instrument_raw, str)
    assert isinstance(contract_year, int)
    assert isinstance(contract_month, int)
    assert isinstance(state_raw, str)
    assert isinstance(created_at, str)
    assert isinstance(updated_at, str)
    return ReversalWorkflowRecord(
        workflow_id=ReversalWorkflowId(UUID(workflow_id_raw)),
        trigger_key=trigger_key,
        account=TradingAccount(branch_id=branch_id, account_no=account_no, sub_account=sub_account),
        instrument=Instrument(instrument_raw),
        contract=ContractMonth(year=contract_year, month=contract_month),
        state=ReversalWorkflowState(state_raw),
        price=None if price_raw is None else Price(Decimal(str(price_raw))),
        starting_net=None if starting_net_lots is None else NetPosition(int(starting_net_lots)),  # type: ignore[call-overload]
        reversal_side=None if reversal_side_raw is None else Side(reversal_side_raw),  # type: ignore[arg-type]
        close_client_order_id=_parse_client_order_id(close_client_order_id_raw),
        entry_client_order_id=_parse_client_order_id(entry_client_order_id_raw),
        pause_reason=None if pause_reason is None else str(pause_reason),
        created_at=_dt(created_at),
        updated_at=_dt(updated_at),
    )


__all__ = ["SqliteReversalWorkflowRepository"]
