"""反手 workflow 狀態機 — the multi-step reversal lifecycle state model.

A reversal is never a single doubled-quantity order — it is always two orders submitted
through `application.order_management.OrderManager` in strict sequence: a close-only
order sized to the *actual, broker-confirmed* starting position, then (only once that
close has been independently re-confirmed flat by a fresh position query, never just the
fill report alone) a fresh 1-lot entry order in the reversed direction.

`PAUSED_SAFE` is deliberately terminal within this state machine — nothing in this
codebase auto-resumes a paused workflow (no resend path exists anywhere, same philosophy
as `domain.order_state_machine.OrderStatus.UNKNOWN`). Moving a paused workflow forward
again is only ever done by `application.reversal_scaling.reversal_service.
ReversalWorkflowService.resume_pending_workflows()` re-deriving fresh state from queries
— never by trusting the paused record itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from uuid import UUID, uuid4

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError, InvalidOrderError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp


class ReversalWorkflowState(StrEnum):
    STARTED = "STARTED"
    POSITION_QUERIED = "POSITION_QUERIED"
    CLOSE_ORDER_SUBMITTED = "CLOSE_ORDER_SUBMITTED"
    CLOSE_FILLED_BY_REPORT = "CLOSE_FILLED_BY_REPORT"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    ENTRY_ORDER_SUBMITTED = "ENTRY_ORDER_SUBMITTED"
    COMPLETED = "COMPLETED"
    PAUSED_SAFE = "PAUSED_SAFE"
    BLOCKED = "BLOCKED"


_INACTIVE_STATES = frozenset({ReversalWorkflowState.COMPLETED, ReversalWorkflowState.BLOCKED})
"""States that free the "one reversal workflow per account/contract" slot.
`PAUSED_SAFE` is deliberately excluded — see `ReversalWorkflowRecord.is_active`."""

_LEGAL_TRANSITIONS: dict[ReversalWorkflowState, frozenset[ReversalWorkflowState]] = {
    ReversalWorkflowState.STARTED: frozenset(
        {
            ReversalWorkflowState.POSITION_QUERIED,
            ReversalWorkflowState.BLOCKED,
            ReversalWorkflowState.PAUSED_SAFE,
        }
    ),
    ReversalWorkflowState.POSITION_QUERIED: frozenset(
        {
            ReversalWorkflowState.CLOSE_ORDER_SUBMITTED,
            ReversalWorkflowState.BLOCKED,
            ReversalWorkflowState.PAUSED_SAFE,
        }
    ),
    ReversalWorkflowState.CLOSE_ORDER_SUBMITTED: frozenset(
        {ReversalWorkflowState.CLOSE_FILLED_BY_REPORT, ReversalWorkflowState.PAUSED_SAFE}
    ),
    ReversalWorkflowState.CLOSE_FILLED_BY_REPORT: frozenset(
        {ReversalWorkflowState.FLAT_CONFIRMED, ReversalWorkflowState.PAUSED_SAFE}
    ),
    ReversalWorkflowState.FLAT_CONFIRMED: frozenset(
        {ReversalWorkflowState.ENTRY_ORDER_SUBMITTED, ReversalWorkflowState.PAUSED_SAFE}
    ),
    ReversalWorkflowState.ENTRY_ORDER_SUBMITTED: frozenset(
        {ReversalWorkflowState.COMPLETED, ReversalWorkflowState.PAUSED_SAFE}
    ),
    ReversalWorkflowState.COMPLETED: frozenset(),
    ReversalWorkflowState.BLOCKED: frozenset(),
    # The one narrow exception to "PAUSED_SAFE is terminal": `application.
    # position_reconciliation.PositionReconciliationService.confirm_manual_sync` marks a
    # paused workflow BLOCKED (freeing the "one reversal workflow per contract" slot) as
    # part of an explicit, human-confirmed position sync — never automatically, and never
    # from anywhere else. This is a deliberate, narrowly-scoped reset, not a resume: the
    # workflow never resumes forward progress, it is simply retired so a fresh reversal
    # can start after the operator restarts the strategy.
    ReversalWorkflowState.PAUSED_SAFE: frozenset({ReversalWorkflowState.BLOCKED}),
}


def can_transition(from_state: ReversalWorkflowState, to_state: ReversalWorkflowState) -> bool:
    return to_state in _LEGAL_TRANSITIONS.get(from_state, frozenset())


@dataclass(frozen=True, slots=True)
class ReversalWorkflowId:
    """This process's own primary key for one reversal workflow — independent of either
    order's `ClientOrderId`."""

    value: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise InvalidOrderError(f"value must be a UUID, got {type(self.value).__name__}")


@dataclass(frozen=True, slots=True)
class ReversalWorkflowRecord:
    """The full local record of one reversal — mutated only via
    `ReversalWorkflowStateMachine`. One row per workflow in persistence."""

    workflow_id: ReversalWorkflowId
    trigger_key: str
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    state: ReversalWorkflowState
    created_at: Timestamp
    updated_at: Timestamp
    price: Price | None = None
    """The reference price both the close and entry legs are submitted at — set once,
    at workflow creation, and reused verbatim on `resume_pending_workflows()` (which has
    no fresh signal/price of its own to work from)."""
    starting_net: NetPosition | None = None
    reversal_side: Side | None = None
    close_client_order_id: ClientOrderId | None = None
    entry_client_order_id: ClientOrderId | None = None
    pause_reason: str | None = None

    @property
    def is_active(self) -> bool:
        """Still occupying the "one reversal workflow per account/contract" slot.
        `PAUSED_SAFE` counts as active — an unresolved reversal must keep blocking a new
        one from starting, not be treated as a green light."""
        return self.state not in _INACTIVE_STATES


@dataclass(frozen=True, slots=True)
class FlatConfirmationResult:
    """The itemized result of the flat-confirmation gate — "查詢結果精確為 0、無活動／
    未知委託、session 與行情皆健康" as four independently-inspectable booleans (the
    "gate 的逐項結果" debug-log requirement), not a single collapsed boolean."""

    is_flat: bool
    position_lots: int
    has_active_or_unknown_orders: bool
    session_healthy: bool
    market_data_healthy: bool

    @property
    def is_confirmed(self) -> bool:
        return (
            self.is_flat
            and not self.has_active_or_unknown_orders
            and self.session_healthy
            and self.market_data_healthy
        )


def reversal_side_for(starting_net: NetPosition) -> Side | None:
    """The side used for *both* legs of a reversal — closing a short means buying (and
    reversing a short into long also means buying one more); closing a long means
    selling (and reversing into short also means selling one more). `None` when already
    flat — nothing to reverse."""
    if starting_net.lots == 0:
        return None
    return Side.BUY if starting_net.lots < 0 else Side.SELL


class ReversalWorkflowStateMachine:
    """Wraps one `ReversalWorkflowRecord`, enforcing the legal-transition table. Never
    does I/O — callers persist `.record` after each successful call."""

    def __init__(self, record: ReversalWorkflowRecord) -> None:
        self._record = record

    @property
    def record(self) -> ReversalWorkflowRecord:
        return self._record

    def mark_position_queried(
        self, *, starting_net: NetPosition, at: Timestamp
    ) -> ReversalWorkflowRecord:
        return self._transition(
            ReversalWorkflowState.POSITION_QUERIED,
            at=at,
            starting_net=starting_net,
            reversal_side=reversal_side_for(starting_net),
        )

    def mark_blocked(self, *, reason: str, at: Timestamp) -> ReversalWorkflowRecord:
        return self._transition(ReversalWorkflowState.BLOCKED, at=at, pause_reason=reason)

    def mark_close_submitted(
        self, *, client_order_id: ClientOrderId, at: Timestamp
    ) -> ReversalWorkflowRecord:
        return self._transition(
            ReversalWorkflowState.CLOSE_ORDER_SUBMITTED,
            at=at,
            close_client_order_id=client_order_id,
        )

    def mark_close_filled(self, *, at: Timestamp) -> ReversalWorkflowRecord:
        return self._transition(ReversalWorkflowState.CLOSE_FILLED_BY_REPORT, at=at)

    def mark_flat_confirmed(self, *, at: Timestamp) -> ReversalWorkflowRecord:
        return self._transition(ReversalWorkflowState.FLAT_CONFIRMED, at=at)

    def mark_entry_submitted(
        self, *, client_order_id: ClientOrderId, at: Timestamp
    ) -> ReversalWorkflowRecord:
        return self._transition(
            ReversalWorkflowState.ENTRY_ORDER_SUBMITTED,
            at=at,
            entry_client_order_id=client_order_id,
        )

    def mark_completed(self, *, at: Timestamp) -> ReversalWorkflowRecord:
        return self._transition(ReversalWorkflowState.COMPLETED, at=at)

    def mark_paused(self, *, reason: str, at: Timestamp) -> ReversalWorkflowRecord:
        return self._transition(ReversalWorkflowState.PAUSED_SAFE, at=at, pause_reason=reason)

    def _transition(
        self, to_state: ReversalWorkflowState, *, at: Timestamp, **updates: object
    ) -> ReversalWorkflowRecord:
        from_state = self._record.state
        if not can_transition(from_state, to_state):
            raise IllegalStateTransitionError(
                f"illegal reversal workflow transition: {from_state.value} -> {to_state.value}"
            )
        self._record = replace(self._record, state=to_state, updated_at=at, **updates)  # type: ignore[arg-type]
        return self._record


__all__ = [
    "FlatConfirmationResult",
    "ReversalWorkflowId",
    "ReversalWorkflowRecord",
    "ReversalWorkflowState",
    "ReversalWorkflowStateMachine",
    "can_transition",
    "reversal_side_for",
]
