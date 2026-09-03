"""SQLite append-only fill ledger; Decimal values are stored as exact text."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal

from tfx_quant.application.ports.fill_ledger_repository import FillAppendOutcome
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trade_report import LedgerFill, PositionEffect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fill_ledger (
 fill_id TEXT PRIMARY KEY, broker_order_no TEXT NOT NULL, order_correlation TEXT NOT NULL,
 masked_account TEXT NOT NULL, instrument TEXT NOT NULL, contract_year INTEGER NOT NULL,
 contract_month INTEGER NOT NULL, side TEXT NOT NULL, position_effect TEXT NOT NULL,
 quantity INTEGER NOT NULL, price TEXT NOT NULL, filled_at TEXT NOT NULL,
 trading_day TEXT NOT NULL, commission TEXT, tax TEXT, source TEXT NOT NULL,
 simulation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fill_ledger_day ON fill_ledger(trading_day, filled_at);
"""


class SqliteFillLedgerRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._lock = threading.Lock()
        with self._lock:
            connection.executescript(_SCHEMA)
            self._migrate_simulation_column()
            connection.commit()

    def _migrate_simulation_column(self) -> None:
        """A ledger file created before the `simulation` provenance column existed keeps
        every pre-existing row (defaulting them to `0` / real) — the column is only ever
        added, never dropped, so an older row can never masquerade as simulated."""
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(fill_ledger)").fetchall()
        }
        if "simulation" not in columns:
            self._connection.execute(
                "ALTER TABLE fill_ledger ADD COLUMN simulation INTEGER NOT NULL DEFAULT 0"
            )

    def append(self, fill: LedgerFill) -> FillAppendOutcome:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO fill_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fill.fill_id,
                    fill.broker_order_no,
                    fill.order_correlation,
                    fill.masked_account,
                    fill.instrument.value,
                    fill.contract.year,
                    fill.contract.month,
                    fill.side.value,
                    fill.position_effect.value,
                    fill.quantity,
                    str(fill.price),
                    fill.filled_at.value.isoformat(),
                    fill.trading_day.isoformat(),
                    None if fill.commission is None else str(fill.commission),
                    None if fill.tax is None else str(fill.tax),
                    fill.source,
                    1 if fill.simulation else 0,
                ),
            )
            self._connection.commit()
        return FillAppendOutcome.INSERTED if cursor.rowcount == 1 else FillAppendOutcome.DUPLICATE

    def list_between(self, start: date, end: date) -> tuple[LedgerFill, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM fill_ledger WHERE trading_day BETWEEN ? AND ? "
                "ORDER BY filled_at, fill_id",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return tuple(_row(row) for row in rows)

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) FROM fill_ledger").fetchone()
        assert row is not None
        return int(row[0])


def _row(row: tuple[object, ...]) -> LedgerFill:
    return LedgerFill(
        fill_id=str(row[0]),
        broker_order_no=str(row[1]),
        order_correlation=str(row[2]),
        masked_account=str(row[3]),
        instrument=Instrument(str(row[4])),
        contract=ContractMonth(int(str(row[5])), int(str(row[6]))),
        side=Side(str(row[7])),
        position_effect=PositionEffect(str(row[8])),
        quantity=int(str(row[9])),
        price=Decimal(str(row[10])),
        filled_at=Timestamp(datetime.fromisoformat(str(row[11]))),
        trading_day=date.fromisoformat(str(row[12])),
        commission=None if row[13] is None else Decimal(str(row[13])),
        tax=None if row[14] is None else Decimal(str(row[14])),
        source=str(row[15]),
        simulation=bool(row[16]),
    )


__all__ = ["SqliteFillLedgerRepository"]
