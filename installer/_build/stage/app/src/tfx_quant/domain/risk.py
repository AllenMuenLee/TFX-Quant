"""風控 — Feature 10's independent, highest-priority risk-supervisor domain model.

Two concerns, deliberately factored out into their own module rather than left solely
inside `domain.strategy_signal_engine`: a bug in the strategy engine's own MA/candle
logic must never be able to bypass either of these, per the implementation prompt's
"獨立於策略的最高優先級 risk supervisor" requirement.

- `is_within_no_entry_window` — the recurring daily `[04:55, 10:45)` band in which no new
  day-session strategy position may be established (08:45/09:45 fall inside it; 10:45 is
  the earliest a new position may open). Risk-driven closes are never blocked by this —
  see `application.risk.gates.validate_entry_window`.
- `EodFlattenWorkflow*` — the persisted, recoverable "close everything now" workflow
  behind both the mandatory 04:55 flatten and the manual emergency-flatten button. Always
  a single CLOSE order sized to the actual, broker-confirmed net position — never a
  blind, guessed-quantity close — followed by a fresh broker position query that must
  come back exactly flat before the workflow is ever reported complete (reuses
  `domain.reversal_workflow.FlatConfirmationResult`/`evaluate_flat_confirmation` — the
  same "查詢結果精確為0、無活動/未知委託、session與行情皆健康" gate
  `application.reversal_scaling.reversal_service.ReversalWorkflowService` already uses
  for its own flat-confirmation step).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError, InvalidOrderError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp

EOD_FLATTEN_LOCAL_TIME = time(4, 55)
ENTRY_GATE_LOCAL_TIME = time(10, 45)


def is_within_no_entry_window(
    now: Timestamp,
    *,
    eod_flatten_local_time: time = EOD_FLATTEN_LOCAL_TIME,
    entry_gate_local_time: time = ENTRY_GATE_LOCAL_TIME,
) -> bool:
    """True during the recurring daily `[eod_flatten, entry_gate)` band (04:55-10:45 by
    default, never crossing midnight) — a plain wall-clock time-of-day comparison, same
    convention as `domain.strategy_signal_engine.StrategySignalEngine._is_entry_window`
    (which this mirrors on purpose, so both call sites independently agree on the same
    band from the same shared definition rather than two separately-hand-copied ones)."""
    t = now.value.time()
    return eod_flatten_local_time <= t < entry_gate_local_time


class EodFlattenTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    """The mandatory, recurring 04:55 forced-flatten — at most one per trading day, per
    account/instrument/contract, edge-triggered on the clock crossing into the no-entry
    window while this process is running."""
    EMERGENCY = "EMERGENCY"
    """The operator-confirmed emergency-flatten button — see the implementation
    prompt's "提供緊急平倉按鈕" requirement. Never auto-triggered."""


class EodFlattenWorkflowState(StrEnum):
    STARTED = "STARTED"
    WAITING_ACTIVE_ORDERS = "WAITING_ACTIVE_ORDERS"
    POSITION_QUERIED = "POSITION_QUERIED"
    CLOSE_ORDER_SUBMITTED = "CLOSE_ORDER_SUBMITTED"
    CLOSE_FILLED_BY_REPORT = "CLOSE_FILLED_BY_REPORT"
    COMPLETED = "COMPLETED"
    ALREADY_FLAT = "ALREADY_FLAT"
    PAUSED_SAFE = "PAUSED_SAFE"


_INACTIVE_STATES = frozenset(
    {EodFlattenWorkflowState.COMPLETED, EodFlattenWorkflowState.ALREADY_FLAT}
)
"""States that free the "one flatten workflow per account/contract" slot. `PAUSED_SAFE`
is deliberately excluded, same reasoning as `domain.reversal_workflow`'s own
`_INACTIVE_STATES` — an unresolved flatten must keep blocking a fresh one (of either
trigger kind) from starting on the same contract, not be treated as a green light."""

_LEGAL_TRANSITIONS: dict[EodFlattenWorkflowState, frozenset[EodFlattenWorkflowState]] = {
    EodFlattenWorkflowState.STARTED: frozenset(
        {
            EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS,
            EodFlattenWorkflowState.ALREADY_FLAT,
            EodFlattenWorkflowState.POSITION_QUERIED,
            EodFlattenWorkflowState.PAUSED_SAFE,
        }
    ),
    EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS: frozenset(
        {
            EodFlattenWorkflowState.ALREADY_FLAT,
            EodFlattenWorkflowState.POSITION_QUERIED,
            EodFlattenWorkflowState.PAUSED_SAFE,
        }
    ),
    EodFlattenWorkflowState.POSITION_QUERIED: frozenset(
        {EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED, EodFlattenWorkflowState.PAUSED_SAFE}
    ),
    EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED: frozenset(
        {EodFlattenWorkflowState.CLOSE_FILLED_BY_REPORT, EodFlattenWorkflowState.PAUSED_SAFE}
    ),
    EodFlattenWorkflowState.CLOSE_FILLED_BY_REPORT: frozenset(
        {EodFlattenWorkflowState.COMPLETED, EodFlattenWorkflowState.PAUSED_SAFE}
    ),
    EodFlattenWorkflowState.COMPLETED: frozenset(),
    EodFlattenWorkflowState.ALREADY_FLAT: frozenset(),
    EodFlattenWorkflowState.PAUSED_SAFE: frozenset(),
}


def can_transition(from_state: EodFlattenWorkflowState, to_state: EodFlattenWorkflowState) -> bool:
    return to_state in _LEGAL_TRANSITIONS.get(from_state, frozenset())


@dataclass(frozen=True, slots=True)
class EodFlattenWorkflowId:
    """This process's own primary key for one flatten workflow — independent of the
    close order's `ClientOrderId`."""

    value: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise InvalidOrderError(f"value must be a UUID, got {type(self.value).__name__}")


@dataclass(frozen=True, slots=True)
class EodFlattenWorkflowRecord:
    """The full local record of one flatten workflow — mutated only via
    `EodFlattenWorkflowStateMachine`. One row per workflow in persistence."""

    workflow_id: EodFlattenWorkflowId
    trigger_key: str
    trigger: EodFlattenTrigger
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    state: EodFlattenWorkflowState
    created_at: Timestamp
    updated_at: Timestamp
    starting_net: NetPosition | None = None
    close_side: Side | None = None
    close_client_order_id: ClientOrderId | None = None
    pause_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state not in _INACTIVE_STATES


def close_side_for(starting_net: NetPosition) -> Side | None:
    """The single side that flattens `starting_net` to zero — `None` when already flat.
    A short is closed by buying, a long by selling. Unlike `domain.reversal_workflow.
    reversal_side_for`, this workflow only ever closes, never reverses."""
    if starting_net.lots == 0:
        return None
    return Side.BUY if starting_net.lots < 0 else Side.SELL


class EodFlattenWorkflowStateMachine:
    """Wraps one `EodFlattenWorkflowRecord`, enforcing the legal-transition table. Never
    does I/O — callers persist `.record` after each successful call."""

    def __init__(self, record: EodFlattenWorkflowRecord) -> None:
        self._record = record

    @property
    def record(self) -> EodFlattenWorkflowRecord:
        return self._record

    def mark_waiting_active_orders(self, *, at: Timestamp) -> EodFlattenWorkflowRecord:
        return self._transition(EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS, at=at)

    def mark_already_flat(self, *, at: Timestamp) -> EodFlattenWorkflowRecord:
        return self._transition(EodFlattenWorkflowState.ALREADY_FLAT, at=at)

    def mark_position_queried(
        self, *, starting_net: NetPosition, at: Timestamp
    ) -> EodFlattenWorkflowRecord:
        return self._transition(
            EodFlattenWorkflowState.POSITION_QUERIED,
            at=at,
            starting_net=starting_net,
            close_side=close_side_for(starting_net),
        )

    def mark_close_submitted(
        self, *, client_order_id: ClientOrderId, at: Timestamp
    ) -> EodFlattenWorkflowRecord:
        return self._transition(
            EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED,
            at=at,
            close_client_order_id=client_order_id,
        )

    def mark_close_filled(self, *, at: Timestamp) -> EodFlattenWorkflowRecord:
        return self._transition(EodFlattenWorkflowState.CLOSE_FILLED_BY_REPORT, at=at)

    def mark_completed(self, *, at: Timestamp) -> EodFlattenWorkflowRecord:
        return self._transition(EodFlattenWorkflowState.COMPLETED, at=at)

    def mark_paused(self, *, reason: str, at: Timestamp) -> EodFlattenWorkflowRecord:
        return self._transition(EodFlattenWorkflowState.PAUSED_SAFE, at=at, pause_reason=reason)

    def _transition(
        self, to_state: EodFlattenWorkflowState, *, at: Timestamp, **updates: Any
    ) -> EodFlattenWorkflowRecord:
        from_state = self._record.state
        if not can_transition(from_state, to_state):
            raise IllegalStateTransitionError(
                f"illegal eod flatten workflow transition: {from_state.value} -> {to_state.value}"
            )
        self._record = replace(self._record, state=to_state, updated_at=at, **updates)
        return self._record


__all__ = [
    "ENTRY_GATE_LOCAL_TIME",
    "EOD_FLATTEN_LOCAL_TIME",
    "EodFlattenTrigger",
    "EodFlattenWorkflowId",
    "EodFlattenWorkflowRecord",
    "EodFlattenWorkflowState",
    "EodFlattenWorkflowStateMachine",
    "can_transition",
    "close_side_for",
    "is_within_no_entry_window",
]
