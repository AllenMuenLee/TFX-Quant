from __future__ import annotations

from datetime import datetime

import pytest

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.risk import (
    EodFlattenTrigger,
    EodFlattenWorkflowId,
    EodFlattenWorkflowRecord,
    EodFlattenWorkflowState,
    EodFlattenWorkflowStateMachine,
    close_side_for,
    is_within_no_entry_window,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
CONTRACT = ContractMonth(year=2026, month=9)
NOW = Timestamp.now()


def _record(
    state: EodFlattenWorkflowState = EodFlattenWorkflowState.STARTED,
) -> EodFlattenWorkflowRecord:
    return EodFlattenWorkflowRecord(
        workflow_id=EodFlattenWorkflowId(),
        trigger_key="key-1",
        trigger=EodFlattenTrigger.SCHEDULED,
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=CONTRACT,
        state=state,
        created_at=NOW,
        updated_at=NOW,
    )


def _at(hour: int, minute: int) -> Timestamp:
    return Timestamp(datetime(2026, 9, 16, hour, minute, tzinfo=TAIPEI_TZ))


# -- is_within_no_entry_window --------------------------------------------------------------


def test_before_0455_is_not_within_window() -> None:
    assert is_within_no_entry_window(_at(4, 54)) is False


def test_exactly_0455_is_within_window() -> None:
    assert is_within_no_entry_window(_at(4, 55)) is True


def test_0845_and_0945_are_within_window() -> None:
    assert is_within_no_entry_window(_at(8, 45)) is True
    assert is_within_no_entry_window(_at(9, 45)) is True


def test_just_before_1045_is_within_window() -> None:
    assert is_within_no_entry_window(_at(10, 44)) is True


def test_exactly_1045_is_not_within_window() -> None:
    assert is_within_no_entry_window(_at(10, 45)) is False


def test_midday_is_not_within_window() -> None:
    assert is_within_no_entry_window(_at(13, 30)) is False


# -- close_side_for ---------------------------------------------------------------------------


def test_close_side_for_short_position_is_buy() -> None:
    assert close_side_for(NetPosition(-1)) is Side.BUY
    assert close_side_for(NetPosition(-2)) is Side.BUY


def test_close_side_for_long_position_is_sell() -> None:
    assert close_side_for(NetPosition(1)) is Side.SELL
    assert close_side_for(NetPosition(2)) is Side.SELL


def test_close_side_for_flat_position_is_none() -> None:
    assert close_side_for(NetPosition(0)) is None


# -- Transitions --------------------------------------------------------------------------


def test_started_to_position_queried_computes_close_side() -> None:
    machine = EodFlattenWorkflowStateMachine(_record())
    record = machine.mark_position_queried(starting_net=NetPosition(-2), at=NOW)
    assert record.state is EodFlattenWorkflowState.POSITION_QUERIED
    assert record.starting_net == NetPosition(-2)
    assert record.close_side is Side.BUY


def test_started_to_already_flat() -> None:
    machine = EodFlattenWorkflowStateMachine(_record())
    record = machine.mark_already_flat(at=NOW)
    assert record.state is EodFlattenWorkflowState.ALREADY_FLAT
    assert record.is_active is False


def test_started_to_waiting_active_orders_to_position_queried() -> None:
    machine = EodFlattenWorkflowStateMachine(_record())
    waiting = machine.mark_waiting_active_orders(at=NOW)
    assert waiting.state is EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS
    assert waiting.is_active is True
    queried = machine.mark_position_queried(starting_net=NetPosition(1), at=NOW)
    assert queried.state is EodFlattenWorkflowState.POSITION_QUERIED


def test_full_happy_path_sequence() -> None:
    machine = EodFlattenWorkflowStateMachine(_record())
    machine.mark_position_queried(starting_net=NetPosition(2), at=NOW)
    client_order_id = ClientOrderId()
    machine.mark_close_submitted(client_order_id=client_order_id, at=NOW)
    machine.mark_close_filled(at=NOW)
    record = machine.mark_completed(at=NOW)
    assert record.state is EodFlattenWorkflowState.COMPLETED
    assert record.close_client_order_id == client_order_id
    assert record.is_active is False


@pytest.mark.parametrize(
    "state",
    [
        EodFlattenWorkflowState.STARTED,
        EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS,
        EodFlattenWorkflowState.POSITION_QUERIED,
        EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED,
        EodFlattenWorkflowState.CLOSE_FILLED_BY_REPORT,
    ],
)
def test_every_active_state_can_pause(state: EodFlattenWorkflowState) -> None:
    machine = EodFlattenWorkflowStateMachine(_record(state))
    record = machine.mark_paused(reason="ambiguous", at=NOW)
    assert record.state is EodFlattenWorkflowState.PAUSED_SAFE


@pytest.mark.parametrize(
    "state",
    [
        EodFlattenWorkflowState.COMPLETED,
        EodFlattenWorkflowState.ALREADY_FLAT,
        EodFlattenWorkflowState.PAUSED_SAFE,
    ],
)
def test_terminal_states_accept_no_further_transition(state: EodFlattenWorkflowState) -> None:
    machine = EodFlattenWorkflowStateMachine(_record(state))
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_paused(reason="whatever", at=NOW)


def test_illegal_skip_ahead_transition_raises() -> None:
    machine = EodFlattenWorkflowStateMachine(_record(EodFlattenWorkflowState.STARTED))
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_close_filled(at=NOW)


# -- is_active ------------------------------------------------------------------------------


def test_paused_safe_counts_as_active() -> None:
    record = _record(EodFlattenWorkflowState.PAUSED_SAFE)
    assert record.is_active is True


@pytest.mark.parametrize(
    "state", [EodFlattenWorkflowState.COMPLETED, EodFlattenWorkflowState.ALREADY_FLAT]
)
def test_completed_and_already_flat_are_not_active(state: EodFlattenWorkflowState) -> None:
    record = _record(state)
    assert record.is_active is False
