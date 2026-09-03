"""SqlitePositionBaselineRepository — the real `PositionBaselineRepository` (SQLite-
backed).

Follows `persistence.sqlite_reversal_workflow_repository.
SqliteReversalWorkflowRepository`'s exact conventions: `threading.Lock` serializing
every statement; its **own** dedicated `sqlite3.Connection`/file — never shares
`orders.sqlite3`'s, `reversal_workflows.sqlite3`'s, or `market_data.sqlite3`'s
connection (two independently-locked repositories over one shared connection would not
actually mutually exclude each other — see `docs/adr/0008-order-and-fill-state-
machine.md` decision 7). One row per (account, instrument, contract), upserted via
`INSERT ... ON CONFLICT DO UPDATE` rather than the insert/update split
`OrderRepository`/`ReversalWorkflowRepository` use, since there is no idempotency-key
dedup concept here — see `docs/adr/0010-position-reconciliation-and-manual-sync.md`.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime

from tfx_quant.application.ports.position_baseline_repository import (
    PositionBaselineRepositoryError,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.position_reconciliation import PositionBaseline
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_debug, log_error

_logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_baselines (
    account_branch_id TEXT NOT NULL,
    account_no TEXT NOT NULL,
    account_sub_account TEXT NOT NULL,
    instrument TEXT NOT NULL,
    contract_year INTEGER NOT NULL,
    contract_month INTEGER NOT NULL,
    expected_net_lots INTEGER NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_branch_id, account_no, account_sub_account, instrument,
                 contract_year, contract_month)
);
"""

_SELECT_COLUMNS = (
    "account_branch_id, account_no, account_sub_account, instrument, contract_year, "
    "contract_month, expected_net_lots, source, updated_at"
)


def _dt(value: str) -> Timestamp:
    return Timestamp(datetime.fromisoformat(value))


class SqlitePositionBaselineRepository:
    """Implements `application.ports.position_baseline_repository.
    PositionBaselineRepository`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> PositionBaseline | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM position_baselines WHERE "
                "account_branch_id = ? AND account_no = ? AND account_sub_account = ? "
                "AND instrument = ? AND contract_year = ? AND contract_month = ?",
                (
                    account.branch_id,
                    account.account_no,
                    account.sub_account,
                    instrument.value,
                    contract.year,
                    contract.month,
                ),
            ).fetchone()
        return None if row is None else _row_to_baseline(row)

    def upsert(self, baseline: PositionBaseline) -> None:
        start = time.monotonic()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO position_baselines (
                        account_branch_id, account_no, account_sub_account, instrument,
                        contract_year, contract_month, expected_net_lots, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (account_branch_id, account_no, account_sub_account,
                                 instrument, contract_year, contract_month)
                    DO UPDATE SET
                        expected_net_lots = excluded.expected_net_lots,
                        source = excluded.source,
                        updated_at = excluded.updated_at
                    """,
                    (
                        baseline.account.branch_id,
                        baseline.account.account_no,
                        baseline.account.sub_account,
                        baseline.instrument.value,
                        baseline.contract.year,
                        baseline.contract.month,
                        baseline.expected_net.lots,
                        baseline.source,
                        baseline.updated_at.value.isoformat(),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            log_error(
                _logger,
                "position_baseline_upsert_failed",
                instrument=baseline.instrument.value,
                contract=baseline.contract.code,
                duration_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise PositionBaselineRepositoryError(
                f"position baseline upsert failed: {exc}"
            ) from exc
        log_debug(
            _logger,
            "position_baseline_upsert_completed",
            instrument=baseline.instrument.value,
            contract=baseline.contract.code,
            expected_net_lots=baseline.expected_net.lots,
            source=baseline.source,
            duration_ms=(time.monotonic() - start) * 1000,
        )


def _row_to_baseline(row: tuple[object, ...]) -> PositionBaseline:
    (
        branch_id,
        account_no,
        sub_account,
        instrument_raw,
        contract_year,
        contract_month,
        expected_net_lots,
        source,
        updated_at,
    ) = row
    assert isinstance(branch_id, str)
    assert isinstance(account_no, str)
    assert isinstance(sub_account, str)
    assert isinstance(instrument_raw, str)
    assert isinstance(contract_year, int)
    assert isinstance(contract_month, int)
    assert isinstance(expected_net_lots, int)
    assert isinstance(source, str)
    assert isinstance(updated_at, str)
    return PositionBaseline(
        account=TradingAccount(branch_id=branch_id, account_no=account_no, sub_account=sub_account),
        instrument=Instrument(instrument_raw),
        contract=ContractMonth(year=contract_year, month=contract_month),
        expected_net=NetPosition(expected_net_lots),
        updated_at=_dt(updated_at),
        source=source,
    )


__all__ = ["SqlitePositionBaselineRepository"]
