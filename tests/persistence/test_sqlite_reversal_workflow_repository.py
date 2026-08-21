from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from tfx_quant.application.ports.reversal_workflow_repository import (
    ReversalWorkflowRepositoryError,
    ReversalWorkflowSaveOutcome,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reversal_workflow import (
    ReversalWorkflowId,
    ReversalWorkflowRecord,
    ReversalWorkflowState,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.persistence.sqlite_reversal_workflow_repository import (
    SqliteReversalWorkflowRepository,
)

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
CONTRACT = ContractMonth(year=2026, month=9)
OTHER_CONTRACT = ContractMonth(year=2026, month=10)
NOW = Timestamp.now()


def _record(
    *,
    trigger_key: str = "key-1",
    state: ReversalWorkflowState = ReversalWorkflowState.STARTED,
    contract: ContractMonth = CONTRACT,
) -> ReversalWorkflowRecord:
    return ReversalWorkflowRecord(
        workflow_id=ReversalWorkflowId(),
        trigger_key=trigger_key,
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=contract,
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def repo() -> SqliteReversalWorkflowRepository:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    return SqliteReversalWorkflowRepository(connection)


def test_save_inserts(repo: SqliteReversalWorkflowRepository) -> None:
    record = _record()
    outcome = repo.save(record)
    assert outcome is ReversalWorkflowSaveOutcome.INSERTED
    found = repo.find_by_workflow_id(record.workflow_id)
    assert found == record


def test_save_duplicate_trigger_key_is_not_inserted_twice(
    repo: SqliteReversalWorkflowRepository,
) -> None:
    first = _record(trigger_key="dup-key")
    repo.save(first)
    second = _record(trigger_key="dup-key")
    outcome = repo.save(second)
    assert outcome is ReversalWorkflowSaveOutcome.DUPLICATE_KEY
    found = repo.find_by_trigger_key("dup-key")
    assert found == first


def test_update_persists_new_state_and_linked_order_ids(
    repo: SqliteReversalWorkflowRepository,
) -> None:
    record = _record()
    repo.save(record)
    close_id = ClientOrderId()
    updated = replace(
        record,
        state=ReversalWorkflowState.CLOSE_ORDER_SUBMITTED,
        starting_net=NetPosition(-2),
        reversal_side=Side.BUY,
        close_client_order_id=close_id,
        updated_at=Timestamp.now(),
    )
    repo.update(updated)
    found = repo.find_by_workflow_id(record.workflow_id)
    assert found is not None
    assert found.state is ReversalWorkflowState.CLOSE_ORDER_SUBMITTED
    assert found.starting_net == NetPosition(-2)
    assert found.reversal_side is Side.BUY
    assert found.close_client_order_id == close_id


def test_update_without_existing_row_raises(repo: SqliteReversalWorkflowRepository) -> None:
    with pytest.raises(ReversalWorkflowRepositoryError):
        repo.update(_record())


def test_find_active_for_contract_excludes_completed_and_blocked(
    repo: SqliteReversalWorkflowRepository,
) -> None:
    active = _record(trigger_key="active", state=ReversalWorkflowState.CLOSE_ORDER_SUBMITTED)
    completed = _record(trigger_key="completed", state=ReversalWorkflowState.COMPLETED)
    blocked = _record(trigger_key="blocked", state=ReversalWorkflowState.BLOCKED)
    repo.save(active)
    repo.save(completed)
    repo.save(blocked)
    found = repo.find_active_for_contract(ACCOUNT, Instrument.MXF, CONTRACT)
    assert [r.trigger_key for r in found] == ["active"]


def test_find_active_for_contract_includes_paused_safe(
    repo: SqliteReversalWorkflowRepository,
) -> None:
    paused = _record(trigger_key="paused", state=ReversalWorkflowState.PAUSED_SAFE)
    repo.save(paused)
    found = repo.find_active_for_contract(ACCOUNT, Instrument.MXF, CONTRACT)
    assert [r.trigger_key for r in found] == ["paused"]


def test_find_active_for_contract_scopes_by_contract(
    repo: SqliteReversalWorkflowRepository,
) -> None:
    this_contract = _record(trigger_key="this", state=ReversalWorkflowState.STARTED)
    other_contract = _record(
        trigger_key="other", state=ReversalWorkflowState.STARTED, contract=OTHER_CONTRACT
    )
    repo.save(this_contract)
    repo.save(other_contract)
    found = repo.find_active_for_contract(ACCOUNT, Instrument.MXF, CONTRACT)
    assert [r.trigger_key for r in found] == ["this"]


def test_list_active_returns_every_non_terminal_workflow(
    repo: SqliteReversalWorkflowRepository,
) -> None:
    a = _record(trigger_key="a", state=ReversalWorkflowState.STARTED, contract=CONTRACT)
    b = _record(trigger_key="b", state=ReversalWorkflowState.PAUSED_SAFE, contract=OTHER_CONTRACT)
    c = _record(trigger_key="c", state=ReversalWorkflowState.COMPLETED, contract=CONTRACT)
    repo.save(a)
    repo.save(b)
    repo.save(c)
    found = {r.trigger_key for r in repo.list_active()}
    assert found == {"a", "b"}


def test_write_failure_after_close_raises_repository_error() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    repo = SqliteReversalWorkflowRepository(connection)
    connection.close()
    with pytest.raises(ReversalWorkflowRepositoryError):
        repo.save(_record())
