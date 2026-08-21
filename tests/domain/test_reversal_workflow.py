from __future__ import annotations

import pytest

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reversal_workflow import (
    ReversalWorkflowId,
    ReversalWorkflowRecord,
    ReversalWorkflowState,
    ReversalWorkflowStateMachine,
    reversal_side_for,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
CONTRACT = ContractMonth(year=2026, month=9)
NOW = Timestamp.now()


def _record(state: ReversalWorkflowState = ReversalWorkflowState.STARTED) -> ReversalWorkflowRecord:
    return ReversalWorkflowRecord(
        workflow_id=ReversalWorkflowId(),
        trigger_key="key-1",
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=CONTRACT,
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


# -- reversal_side_for --------------------------------------------------------------------


def test_reversal_side_for_short_position_is_buy() -> None:
    assert reversal_side_for(NetPosition(-1)) is Side.BUY
    assert reversal_side_for(NetPosition(-2)) is Side.BUY


def test_reversal_side_for_long_position_is_sell() -> None:
    assert reversal_side_for(NetPosition(1)) is Side.SELL
    assert reversal_side_for(NetPosition(2)) is Side.SELL


def test_reversal_side_for_flat_position_is_none() -> None:
    assert reversal_side_for(NetPosition(0)) is None


# -- Transitions --------------------------------------------------------------------------


def test_started_to_position_queried_computes_reversal_side() -> None:
    machine = ReversalWorkflowStateMachine(_record())
    record = machine.mark_position_queried(starting_net=NetPosition(-2), at=NOW)
    assert record.state is ReversalWorkflowState.POSITION_QUERIED
    assert record.starting_net == NetPosition(-2)
    assert record.reversal_side is Side.BUY


def test_started_to_blocked() -> None:
    machine = ReversalWorkflowStateMachine(_record())
    record = machine.mark_blocked(reason="已無持倉，無需反手", at=NOW)
    assert record.state is ReversalWorkflowState.BLOCKED
    assert record.pause_reason == "已無持倉，無需反手"


def test_position_queried_to_close_order_submitted() -> None:
    machine = ReversalWorkflowStateMachine(_record(ReversalWorkflowState.POSITION_QUERIED))
    client_order_id = ClientOrderId()
    record = machine.mark_close_submitted(client_order_id=client_order_id, at=NOW)
    assert record.state is ReversalWorkflowState.CLOSE_ORDER_SUBMITTED
    assert record.close_client_order_id == client_order_id


def test_full_happy_path_sequence() -> None:
    machine = ReversalWorkflowStateMachine(_record())
    machine.mark_position_queried(starting_net=NetPosition(1), at=NOW)
    machine.mark_close_submitted(client_order_id=ClientOrderId(), at=NOW)
    machine.mark_close_filled(at=NOW)
    machine.mark_flat_confirmed(at=NOW)
    entry_id = ClientOrderId()
    machine.mark_entry_submitted(client_order_id=entry_id, at=NOW)
    record = machine.mark_completed(at=NOW)
    assert record.state is ReversalWorkflowState.COMPLETED
    assert record.entry_client_order_id == entry_id


@pytest.mark.parametrize(
    "state",
    [
        ReversalWorkflowState.STARTED,
        ReversalWorkflowState.POSITION_QUERIED,
        ReversalWorkflowState.CLOSE_ORDER_SUBMITTED,
        ReversalWorkflowState.CLOSE_FILLED_BY_REPORT,
        ReversalWorkflowState.FLAT_CONFIRMED,
        ReversalWorkflowState.ENTRY_ORDER_SUBMITTED,
    ],
)
def test_every_active_state_can_pause(state: ReversalWorkflowState) -> None:
    machine = ReversalWorkflowStateMachine(_record(state))
    record = machine.mark_paused(reason="ambiguous", at=NOW)
    assert record.state is ReversalWorkflowState.PAUSED_SAFE


@pytest.mark.parametrize(
    "state",
    [
        ReversalWorkflowState.COMPLETED,
        ReversalWorkflowState.BLOCKED,
        ReversalWorkflowState.PAUSED_SAFE,
    ],
)
def test_terminal_states_accept_no_further_transition(state: ReversalWorkflowState) -> None:
    machine = ReversalWorkflowStateMachine(_record(state))
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_paused(reason="whatever", at=NOW)


def test_illegal_skip_ahead_transition_raises() -> None:
    machine = ReversalWorkflowStateMachine(_record(ReversalWorkflowState.STARTED))
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_close_filled(at=NOW)


def test_paused_safe_can_be_marked_blocked_for_manual_sync_reset() -> None:
    """The one narrow exception to "PAUSED_SAFE is terminal" — see
    `application.position_reconciliation.reconciliation_service.
    PositionReconciliationService._reset_paused_reversal_workflows`. Never resumes
    forward progress, only retires the workflow so the "one workflow per contract" slot
    frees up."""
    machine = ReversalWorkflowStateMachine(_record(ReversalWorkflowState.PAUSED_SAFE))
    record = machine.mark_blocked(reason="position reconciliation manual sync reset", at=NOW)
    assert record.state is ReversalWorkflowState.BLOCKED
    assert record.is_active is False


@pytest.mark.parametrize("state", [ReversalWorkflowState.COMPLETED, ReversalWorkflowState.BLOCKED])
def test_other_terminal_states_still_cannot_reach_blocked(state: ReversalWorkflowState) -> None:
    machine = ReversalWorkflowStateMachine(_record(state))
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_blocked(reason="whatever", at=NOW)


# -- is_active ------------------------------------------------------------------------------


def test_paused_safe_counts_as_active() -> None:
    record = _record(ReversalWorkflowState.PAUSED_SAFE)
    assert record.is_active is True


@pytest.mark.parametrize("state", [ReversalWorkflowState.COMPLETED, ReversalWorkflowState.BLOCKED])
def test_completed_and_blocked_are_not_active(state: ReversalWorkflowState) -> None:
    record = _record(state)
    assert record.is_active is False
