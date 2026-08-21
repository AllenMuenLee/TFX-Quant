from __future__ import annotations

import sqlite3

import pytest

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.position_reconciliation import PositionBaseline
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.persistence.sqlite_position_baseline_repository import (
    SqlitePositionBaselineRepository,
)

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
CONTRACT = ContractMonth(year=2026, month=9)
OTHER_CONTRACT = ContractMonth(year=2026, month=10)
NOW = Timestamp.now()


def _baseline(
    *,
    contract: ContractMonth = CONTRACT,
    lots: int = 0,
    source: str = "assumed_flat_at_first_use",
    updated_at: Timestamp = NOW,
) -> PositionBaseline:
    return PositionBaseline(
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=contract,
        expected_net=NetPosition(lots),
        updated_at=updated_at,
        source=source,
    )


@pytest.fixture
def repo() -> SqlitePositionBaselineRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SqlitePositionBaselineRepository(connection)


def test_get_returns_none_when_no_row_exists(repo: SqlitePositionBaselineRepository) -> None:
    assert repo.get(ACCOUNT, Instrument.MXF, CONTRACT) is None


def test_upsert_then_get_round_trips(repo: SqlitePositionBaselineRepository) -> None:
    baseline = _baseline(lots=1, source="fill")
    repo.upsert(baseline)

    loaded = repo.get(ACCOUNT, Instrument.MXF, CONTRACT)

    assert loaded == baseline


def test_upsert_replaces_existing_row_for_same_key(repo: SqlitePositionBaselineRepository) -> None:
    repo.upsert(_baseline(lots=1, source="fill"))
    repo.upsert(_baseline(lots=2, source="manual_sync"))

    loaded = repo.get(ACCOUNT, Instrument.MXF, CONTRACT)

    assert loaded is not None
    assert loaded.expected_net.lots == 2
    assert loaded.source == "manual_sync"


def test_baselines_for_different_contracts_are_independent(
    repo: SqlitePositionBaselineRepository,
) -> None:
    repo.upsert(_baseline(contract=CONTRACT, lots=1))
    repo.upsert(_baseline(contract=OTHER_CONTRACT, lots=-2))

    first = repo.get(ACCOUNT, Instrument.MXF, CONTRACT)
    second = repo.get(ACCOUNT, Instrument.MXF, OTHER_CONTRACT)

    assert first is not None and first.expected_net.lots == 1
    assert second is not None and second.expected_net.lots == -2
