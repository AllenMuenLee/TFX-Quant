"""委託狀態機 — the order/fill lifecycle state model.

Broker order reports and fill reports are the only source of truth (never the result of
an API call, never market data). Every transition here is driven by an already-parsed
`OrderReport`/`Fill`, deduped and ordered by a single per-order monotonic
`broker_seq_no` gate shared between both report kinds — an incoming report/fill whose
`broker_seq_no` does not exceed the last one actually applied is a duplicate or
out-of-order replay, logged by the caller and otherwise ignored.

`UNKNOWN` deliberately blocks all automatic control flow (no resend is ever possible from
here — there is no resend code path in this codebase at all; a fresh order is always a
brand new `OrderManager.submit()` call from a human/operator action) but is **not**
terminal for *recording* an authoritative broker fact: a late order report or late fill
arriving after a timeout pushed an order to `UNKNOWN` must still be applied (see
`_LEGAL_ORDER_REPORT_TRANSITIONS` and `apply_fill`'s permissive source-state check) so the
accounting stays correct even though the automated workflow has already given up.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError, InvalidOrderError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tfx_quant.domain.fill import Fill


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


_TERMINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})
"""Statuses with no further legal transition at all — not even to `UNKNOWN`. `UNKNOWN`
itself is deliberately excluded: it is a dead end for automatic control flow but a late,
authoritative broker fact must still be recordable from it."""

_LEGAL_ORDER_REPORT_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {
            OrderStatus.SUBMITTING,
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.SUBMITTING: frozenset(
        {OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.UNKNOWN}
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCELLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


def can_transition(from_status: OrderStatus, to_status: OrderStatus) -> bool:
    return to_status in _LEGAL_ORDER_REPORT_TRANSITIONS.get(from_status, frozenset())


@dataclass(frozen=True, slots=True)
class LocalOrderId:
    """This process's own primary key for one order intent — independent of the
    broker-facing `ClientOrderId` idempotency key and of the broker's own order number."""

    value: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise InvalidOrderError(f"value must be a UUID, got {type(self.value).__name__}")


@dataclass(frozen=True, slots=True)
class OrderReport:
    """One broker order-level report snapshot (ack/reject/cancel-confirm/...).

    `broker_seq_no` is a per-order monotonic sequence number this codebase assumes the
    broker (or, until a real vendor adapter exists, the mock adapter) assigns — real SPARK
    API order-report sequencing is unverified; see docs/adr/0008."""

    client_order_id: ClientOrderId
    status: OrderStatus
    broker_seq_no: int
    at: Timestamp
    broker_order_no: str | None = None
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """The full local record of one order — the "為每張委託保存..." data-retention
    requirement. One row per order in persistence; mutated only via `OrderStateMachine`."""

    local_order_id: LocalOrderId
    client_order_id: ClientOrderId
    workflow_id: str
    idempotency_key: str
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    side: Side
    kind: OrderKind
    quantity: Quantity
    status: OrderStatus
    created_at: Timestamp
    updated_at: Timestamp
    broker_order_no: str | None = None
    filled_quantity: int = 0
    avg_fill_price: Price | None = None
    last_applied_broker_seq_no: int | None = None
    last_event_summary: str = ""
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if not (0 <= self.filled_quantity <= self.quantity.lots):
            raise InvalidOrderError(
                f"filled_quantity must be between 0 and {self.quantity.lots}, "
                f"got {self.filled_quantity}"
            )
        if self.filled_quantity == 0 and self.avg_fill_price is not None:
            raise InvalidOrderError("avg_fill_price must be None when filled_quantity is 0")
        if self.filled_quantity > 0 and self.avg_fill_price is None:
            raise InvalidOrderError("avg_fill_price is required once filled_quantity > 0")

    @property
    def remaining_quantity(self) -> int:
        return self.quantity.lots - self.filled_quantity

    @property
    def is_active(self) -> bool:
        """Still occupying the "one workflow per account/contract" slot. `UNKNOWN` counts
        as active — an unresolved order must keep blocking new submissions on the same
        contract, not be treated as a green light."""
        return self.status not in _TERMINAL_STATUSES


def worst_case_net_position_range(
    current: NetPosition, active: Sequence[OrderIntent]
) -> tuple[int, int]:
    """The `[min, max]` net position reachable across every possible fill outcome of every
    still-active order, starting from `current`. Each active order's *remaining* quantity
    is assumed independently able to fully fill in its own direction — the "使用送單前
    持倉加上所有可能成交的活動委託計算最壞曝險" requirement. General over a sequence even
    though `OrderManager`'s "one workflow per account/contract" rule normally keeps it to
    at most one entry — defensive, not an assumed invariant."""
    lo = hi = current.lots
    for intent in active:
        if not intent.is_active or intent.remaining_quantity <= 0:
            continue
        remaining = intent.remaining_quantity
        signed = remaining if intent.side is Side.BUY else -remaining
        if signed > 0:
            hi += signed
        else:
            lo += signed
    return lo, hi


class OrderStateMachine:
    """Wraps one `OrderIntent`, enforcing the legal-transition table and the
    duplicate/out-of-order dedup gate. Never does I/O — callers persist `.record` after
    each successful call."""

    def __init__(self, intent: OrderIntent) -> None:
        self._record = intent

    @property
    def record(self) -> OrderIntent:
        return self._record

    def mark_submitting(self, *, at: Timestamp) -> OrderIntent:
        return self._transition(OrderStatus.SUBMITTING, at=at, trigger="submit")

    def mark_cancel_pending(self, *, at: Timestamp) -> OrderIntent:
        if self._record.status not in (OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED):
            raise IllegalStateTransitionError(
                f"cannot request cancel from status {self._record.status.value}"
            )
        return self._transition(OrderStatus.CANCEL_PENDING, at=at, trigger="cancel_requested")

    def mark_unknown(self, *, at: Timestamp, reason: str) -> OrderIntent:
        if self._record.status in _TERMINAL_STATUSES:
            raise IllegalStateTransitionError(
                f"cannot mark UNKNOWN from terminal status {self._record.status.value}"
            )
        self._record = replace(
            self._record,
            status=OrderStatus.UNKNOWN,
            updated_at=at,
            last_event_summary=f"UNKNOWN: {reason}",
        )
        return self._record

    def apply_order_report(self, report: OrderReport) -> tuple[OrderIntent, bool]:
        """Returns `(record, applied)`. `applied=False` means the report was a duplicate
        or out-of-order replay (by `broker_seq_no`) and was ignored — the caller is
        expected to log this, not raise."""
        if not self._is_new_seq(report.broker_seq_no):
            return self._record, False
        if not can_transition(self._record.status, report.status):
            raise IllegalStateTransitionError(
                f"illegal order-report transition: {self._record.status.value} -> "
                f"{report.status.value}"
            )
        self._record = replace(
            self._record,
            status=report.status,
            updated_at=report.at,
            broker_order_no=report.broker_order_no or self._record.broker_order_no,
            reject_reason=report.reject_reason,
            last_applied_broker_seq_no=report.broker_seq_no,
            last_event_summary=(
                f"order_report seq={report.broker_seq_no} status={report.status.value}"
            ),
        )
        return self._record, True

    def apply_fill(self, fill: Fill, *, broker_seq_no: int) -> tuple[OrderIntent, bool]:
        """Returns `(record, applied)`. Deliberately permissive about the *source*
        status — legal from anything non-terminal-and-full, including `UNKNOWN`, since a
        fill is an authoritative broker fact that must always be recordable. An impossible
        fill (would overfill, or arrives for an already-FILLED/REJECTED/CANCELLED order)
        is never applied; the caller is expected to mark the order `UNKNOWN` instead."""
        if not self._is_new_seq(broker_seq_no):
            return self._record, False
        record = self._record
        if record.status in _TERMINAL_STATUSES:
            return record, False
        new_filled = record.filled_quantity + fill.quantity.lots
        if new_filled > record.quantity.lots:
            return record, False
        new_avg = _weighted_avg_price(record, fill)
        is_fully_filled = new_filled == record.quantity.lots
        new_status = OrderStatus.FILLED if is_fully_filled else OrderStatus.PARTIALLY_FILLED
        self._record = replace(
            record,
            status=new_status,
            filled_quantity=new_filled,
            avg_fill_price=new_avg,
            updated_at=fill.at,
            last_applied_broker_seq_no=broker_seq_no,
            last_event_summary=(
                f"fill seq={broker_seq_no} fill_no={fill.broker_fill_no} qty={fill.quantity.lots}"
            ),
        )
        return self._record, True

    def _is_new_seq(self, seq: int) -> bool:
        last = self._record.last_applied_broker_seq_no
        return last is None or seq > last

    def _transition(self, to_status: OrderStatus, *, at: Timestamp, trigger: str) -> OrderIntent:
        from_status = self._record.status
        if not can_transition(from_status, to_status):
            raise IllegalStateTransitionError(
                f"illegal order transition ({trigger}): {from_status.value} -> {to_status.value}"
            )
        self._record = replace(
            self._record, status=to_status, updated_at=at, last_event_summary=trigger
        )
        return self._record


def _weighted_avg_price(record: OrderIntent, fill: Fill) -> Price:
    if record.filled_quantity == 0 or record.avg_fill_price is None:
        return fill.price
    prior_notional = record.avg_fill_price.amount * record.filled_quantity
    fill_notional = fill.price.amount * fill.quantity.lots
    total_qty = record.filled_quantity + fill.quantity.lots
    return Price((prior_notional + fill_notional) / total_qty)


__all__ = [
    "LocalOrderId",
    "OrderIntent",
    "OrderReport",
    "OrderStateMachine",
    "OrderStatus",
    "can_transition",
    "worst_case_net_position_range",
]
