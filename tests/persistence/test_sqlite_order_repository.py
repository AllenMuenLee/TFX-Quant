from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from tfx_quant.application.ports.order_repository import (
    OrderIntentSaveOutcome,
    OrderRepositoryError,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId, OrderKind
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderIntent, OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
OTHER_CONTRACT = ContractMonth(year=2026, month=10)
CONTRACT = ContractMonth(year=2026, month=9)
NOW = Timestamp.now()


def _intent(
    *,
    idempotency_key: str = "key-1",
    status: OrderStatus = OrderStatus.CREATED,
    account: TradingAccount = ACCOUNT,
    contract: ContractMonth = CONTRACT,
) -> OrderIntent:
    return OrderIntent(
        local_order_id=LocalOrderId(),
        client_order_id=ClientOrderId(),
        workflow_id="wf-1",
        idempotency_key=idempotency_key,
        account=account,
        instrument=Instrument.MXF,
        contract=contract,
        side=Side.BUY,
        kind=OrderKind.OPEN,
        quantity=Quantity(1),
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def repo() -> SqliteOrderRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteOrderRepository(connection)


def test_save_intent_inserts(repo: SqliteOrderRepository) -> None:
    intent = replace(_intent(), reason="均線突破進場")
    outcome = repo.save_intent(intent)
    assert outcome is OrderIntentSaveOutcome.INSERTED
    found = repo.find_by_local_order_id(intent.local_order_id)
    assert found == intent


def test_existing_database_is_migrated_with_empty_reason() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute(
        """
        CREATE TABLE order_intents (
            local_order_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL UNIQUE, workflow_id TEXT NOT NULL,
            account_branch_id TEXT NOT NULL, account_no TEXT NOT NULL,
            account_sub_account TEXT NOT NULL, instrument TEXT NOT NULL,
            contract_year INTEGER NOT NULL, contract_month INTEGER NOT NULL,
            side TEXT NOT NULL, kind TEXT NOT NULL, quantity_lots INTEGER NOT NULL,
            status TEXT NOT NULL, broker_order_no TEXT, filled_quantity INTEGER NOT NULL,
            avg_fill_price TEXT, last_applied_broker_seq_no INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            last_event_summary TEXT NOT NULL, reject_reason TEXT
        )
        """
    )
    connection.commit()

    repo = SqliteOrderRepository(connection)
    intent = _intent()
    assert repo.save_intent(intent) is OrderIntentSaveOutcome.INSERTED
    assert repo.find_by_local_order_id(intent.local_order_id) == intent
    columns = {row[1] for row in connection.execute("PRAGMA table_info(order_intents)")}
    assert {"reason", "triggered_at", "requested_price", "time_in_force"} <= columns


def test_save_intent_duplicate_idempotency_key_is_not_inserted_twice(
    repo: SqliteOrderRepository,
) -> None:
    first = _intent(idempotency_key="dup-key")
    repo.save_intent(first)
    second = _intent(idempotency_key="dup-key")
    outcome = repo.save_intent(second)
    assert outcome is OrderIntentSaveOutcome.DUPLICATE_KEY
    found = repo.find_by_idempotency_key("dup-key")
    assert found == first  # the original row, never overwritten


def test_find_by_client_order_id(repo: SqliteOrderRepository) -> None:
    intent = _intent()
    repo.save_intent(intent)
    found = repo.find_by_client_order_id(intent.client_order_id)
    assert found == intent


def test_find_by_local_order_id_missing_returns_none(repo: SqliteOrderRepository) -> None:
    assert repo.find_by_local_order_id(LocalOrderId()) is None


def test_update_intent_persists_new_status(repo: SqliteOrderRepository) -> None:
    intent = _intent()
    repo.save_intent(intent)
    updated = replace(intent, status=OrderStatus.SUBMITTING, updated_at=Timestamp.now())
    repo.update_intent(updated)
    found = repo.find_by_local_order_id(intent.local_order_id)
    assert found is not None
    assert found.status is OrderStatus.SUBMITTING


def test_update_intent_without_existing_row_raises(repo: SqliteOrderRepository) -> None:
    intent = _intent()
    with pytest.raises(OrderRepositoryError):
        repo.update_intent(intent)


def test_find_active_for_contract_excludes_terminal_statuses(repo: SqliteOrderRepository) -> None:
    active = _intent(idempotency_key="active", status=OrderStatus.ACKNOWLEDGED)
    filled = _intent(idempotency_key="filled", status=OrderStatus.FILLED)
    repo.save_intent(active)
    repo.save_intent(filled)
    found = repo.find_active_for_contract(ACCOUNT, Instrument.MXF, CONTRACT)
    assert [r.idempotency_key for r in found] == ["active"]


def test_find_active_for_contract_includes_unknown_status(repo: SqliteOrderRepository) -> None:
    unknown = _intent(idempotency_key="unknown-1", status=OrderStatus.UNKNOWN)
    repo.save_intent(unknown)
    found = repo.find_active_for_contract(ACCOUNT, Instrument.MXF, CONTRACT)
    assert [r.idempotency_key for r in found] == ["unknown-1"]


def test_find_active_for_contract_scopes_by_contract(repo: SqliteOrderRepository) -> None:
    this_contract = _intent(
        idempotency_key="this", status=OrderStatus.ACKNOWLEDGED, contract=CONTRACT
    )
    other_contract = _intent(
        idempotency_key="other", status=OrderStatus.ACKNOWLEDGED, contract=OTHER_CONTRACT
    )
    repo.save_intent(this_contract)
    repo.save_intent(other_contract)
    found = repo.find_active_for_contract(ACCOUNT, Instrument.MXF, CONTRACT)
    assert [r.idempotency_key for r in found] == ["this"]


def test_list_active_returns_every_non_terminal_intent(repo: SqliteOrderRepository) -> None:
    a = _intent(idempotency_key="a", status=OrderStatus.ACKNOWLEDGED, contract=CONTRACT)
    b = _intent(idempotency_key="b", status=OrderStatus.CANCEL_PENDING, contract=OTHER_CONTRACT)
    c = _intent(idempotency_key="c", status=OrderStatus.CANCELLED, contract=CONTRACT)
    repo.save_intent(a)
    repo.save_intent(b)
    repo.save_intent(c)
    found = {r.idempotency_key for r in repo.list_active()}
    assert found == {"a", "b"}


def test_write_failure_after_close_raises_repository_error() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repo = SqliteOrderRepository(connection)
    connection.close()
    with pytest.raises(OrderRepositoryError):
        repo.save_intent(_intent())
