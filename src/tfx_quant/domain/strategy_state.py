"""策略狀態 — the strategy engine's lifecycle state machine.

`Running` is only reachable from `Starting` — never directly from `Stopped` or
`PausedSafe` — so entering live trading always passes through the startup safety
checks that gate the Starting→Running transition (see
`application.safety.startup_safety_gate`). Resuming from `PausedSafe` therefore goes
back through `Starting` (re-running the full safety checklist) rather than straight to
`Running`.
"""

from __future__ import annotations

from enum import StrEnum

from tfx_quant.domain.errors import IllegalStateTransitionError
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)


class StrategyState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED_SAFE = "PAUSED_SAFE"
    STOPPING = "STOPPING"
    FAULTED = "FAULTED"


_LEGAL_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.STOPPED: frozenset({StrategyState.STARTING}),
    StrategyState.STARTING: frozenset(
        {StrategyState.RUNNING, StrategyState.STOPPED, StrategyState.FAULTED}
    ),
    StrategyState.RUNNING: frozenset(
        {StrategyState.PAUSED_SAFE, StrategyState.STOPPING, StrategyState.FAULTED}
    ),
    StrategyState.PAUSED_SAFE: frozenset(
        {StrategyState.STARTING, StrategyState.STOPPING, StrategyState.FAULTED}
    ),
    StrategyState.STOPPING: frozenset({StrategyState.STOPPED, StrategyState.FAULTED}),
    StrategyState.FAULTED: frozenset({StrategyState.STOPPED}),
}


class StrategyStateMachine:
    """Enforces the legal transition table above; starts in `Stopped`."""

    def __init__(self, initial: StrategyState = StrategyState.STOPPED) -> None:
        self._state = initial

    @property
    def state(self) -> StrategyState:
        return self._state

    @staticmethod
    def can_transition(from_state: StrategyState, to_state: StrategyState) -> bool:
        return to_state in _LEGAL_TRANSITIONS.get(from_state, frozenset())

    def transition(self, to_state: StrategyState) -> None:
        from_state = self._state
        if not self.can_transition(from_state, to_state):
            log_warning(
                _logger,
                "strategy_state_transition_rejected",
                from_state=from_state.value,
                to_state=to_state.value,
                reason="not in legal transition table",
            )
            raise IllegalStateTransitionError(
                f"illegal strategy state transition: {from_state.value} -> {to_state.value}"
            )
        self._state = to_state
        log_info(
            _logger,
            "strategy_state_transitioned",
            from_state=from_state.value,
            to_state=to_state.value,
        )
