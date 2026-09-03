from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

from tfx_quant.application.ports.fill_ledger_repository import FillAppendOutcome
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trade_report import LedgerFill, PositionEffect
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository

_CONTRACT = ContractMonth(2026, 9)
_DAY = date(2026, 8, 25)


def _fill(fill_id: str, *, simulation: bool) -> LedgerFill:
    return LedgerFill(
        fill_id=fill_id,
        broker_order_no=f"B-{fill_id}",
        order_correlation=f"wf-{fill_id}",
        masked_account="***4567",
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=Side.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=1,
        price=Decimal("18500"),
        filled_at=Timestamp(datetime(2026, 8, 25, 10, tzinfo=TAIPEI_TZ)),
        trading_day=_DAY,
        commission=Decimal("20"),
        tax=Decimal("5"),
        source="SIMULATION" if simulation else "YUANTA_OCX",
        simulation=simulation,
    )


def test_simulation_column_round_trips() -> None:
    repo = SqliteFillLedgerRepository(sqlite3.connect(":memory:", check_same_thread=False))
    repo.append(_fill("sim", simulation=True))
    repo.append(_fill("real", simulation=False))

    rows = {f.fill_id: f for f in repo.list_between(_DAY, _DAY)}
    assert rows["sim"].simulation is True
    assert rows["real"].simulation is False


def test_migration_adds_simulation_column_to_a_pre_existing_table(tmp_path) -> None:
    db = tmp_path / "legacy_fill_ledger.sqlite3"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE fill_ledger (
         fill_id TEXT PRIMARY KEY, broker_order_no TEXT NOT NULL, order_correlation TEXT NOT NULL,
         masked_account TEXT NOT NULL, instrument TEXT NOT NULL, contract_year INTEGER NOT NULL,
         contract_month INTEGER NOT NULL, side TEXT NOT NULL, position_effect TEXT NOT NULL,
         quantity INTEGER NOT NULL, price TEXT NOT NULL, filled_at TEXT NOT NULL,
         trading_day TEXT NOT NULL, commission TEXT, tax TEXT, source TEXT NOT NULL
        );
        INSERT INTO fill_ledger VALUES
         ('old','B-old','wf-old','***4567','MXF',2026,9,'BUY','OPEN',1,'18500',
          '2026-08-25T10:00:00+08:00','2026-08-25','20','5','YUANTA_OCX');
        """
    )
    legacy.commit()
    legacy.close()

    repo = SqliteFillLedgerRepository(sqlite3.connect(db))
    # pre-existing row survives, defaulted to real
    old = list(repo.list_between(_DAY, _DAY))
    assert len(old) == 1
    assert old[0].simulation is False
    # new writes carry the flag
    assert repo.append(_fill("new", simulation=True)) is FillAppendOutcome.INSERTED
    rows = {f.fill_id: f for f in repo.list_between(_DAY, _DAY)}
    assert rows["new"].simulation is True


def test_duplicate_fill_id_is_reported_not_raised() -> None:
    repo = SqliteFillLedgerRepository(sqlite3.connect(":memory:", check_same_thread=False))
    assert repo.append(_fill("dup", simulation=True)) is FillAppendOutcome.INSERTED
    assert repo.append(_fill("dup", simulation=True)) is FillAppendOutcome.DUPLICATE
    assert repo.count() == 1
