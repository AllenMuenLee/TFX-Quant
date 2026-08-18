from __future__ import annotations

import itertools

import pytest

from tfx_quant.domain.errors import IllegalStateTransitionError
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine

LEGAL_TRANSITIONS = {
    (StrategyState.STOPPED, StrategyState.STARTING),
    (StrategyState.STARTING, StrategyState.RUNNING),
    (StrategyState.STARTING, StrategyState.STOPPED),
    (StrategyState.STARTING, StrategyState.FAULTED),
    (StrategyState.RUNNING, StrategyState.PAUSED_SAFE),
    (StrategyState.RUNNING, StrategyState.STOPPING),
    (StrategyState.RUNNING, StrategyState.FAULTED),
    (StrategyState.PAUSED_SAFE, StrategyState.STARTING),
    (StrategyState.PAUSED_SAFE, StrategyState.STOPPING),
    (StrategyState.PAUSED_SAFE, StrategyState.FAULTED),
    (StrategyState.STOPPING, StrategyState.STOPPED),
    (StrategyState.STOPPING, StrategyState.FAULTED),
    (StrategyState.FAULTED, StrategyState.STOPPED),
}

ALL_PAIRS = set(itertools.product(StrategyState, StrategyState))
ILLEGAL_TRANSITIONS = ALL_PAIRS - LEGAL_TRANSITIONS


@pytest.mark.parametrize("from_state,to_state", sorted(LEGAL_TRANSITIONS, key=str))
def test_legal_transitions_are_allowed(from_state: StrategyState, to_state: StrategyState) -> None:
    machine = StrategyStateMachine(initial=from_state)
    machine.transition(to_state)
    assert machine.state is to_state


@pytest.mark.parametrize("from_state,to_state", sorted(ILLEGAL_TRANSITIONS, key=str))
def test_illegal_transitions_are_rejected(
    from_state: StrategyState, to_state: StrategyState
) -> None:
    machine = StrategyStateMachine(initial=from_state)
    with pytest.raises(IllegalStateTransitionError):
        machine.transition(to_state)
    assert machine.state is from_state, "a rejected transition must not mutate state"


def test_running_is_only_reachable_from_starting() -> None:
    for from_state in StrategyState:
        can_reach = StrategyStateMachine.can_transition(from_state, StrategyState.RUNNING)
        assert can_reach == (from_state is StrategyState.STARTING)


def test_new_machine_starts_stopped() -> None:
    assert StrategyStateMachine().state is StrategyState.STOPPED
