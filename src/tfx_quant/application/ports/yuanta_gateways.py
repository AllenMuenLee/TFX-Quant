"""Yuanta broker trade gateway port.

Feature 01 defined the readiness/query surface needed by the `StartupSafetyGate`
checklist (login state, order/position sync). Feature 06 adds order submission —
`submit_order`/`cancel_order` are async, fire-and-forget calls (same convention as
`IBrokerSession.start()`): the result never arrives as this call's return value, only via
the `OrderReportReceived`/`FillReceived` events an implementation publishes later.
`query_order_reports`/`query_fills` back the startup/reconnect reconciliation sweep in
`application.order_management.order_manager.OrderManager`.

Only `infrastructure.yuanta` may ever import vendor COM/ActiveX types;
everything else, including the desktop composition root, depends on this Protocol. There
is no quote/market-data surface in this trading port; quotes use the separate
`QuoteGateway` port.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tfx_quant.domain.fill import Fill
from tfx_quant.domain.order import ClientOrderId, Order
from tfx_quant.domain.order_state_machine import OrderReport
from tfx_quant.domain.position import Position


class OrderQueryNotReadyError(RuntimeError):
    """`query_open_orders()` cannot yet give a safe answer — either the broker's order
    query has never completed for this session, or an order's status could not be
    resolved. Callers must fail closed and never assume "no open orders"."""


class TradeGatewayPort(Protocol):
    def is_logged_in(self) -> bool: ...

    def query_open_orders(self) -> Sequence[Order]:
        """Used by the safety checklist's "no unknown orders" check. Must raise
        `OrderQueryNotReadyError` rather than returning an empty sequence whenever
        "zero open orders" isn't a confirmed fact yet."""
        ...

    def query_positions(self) -> Sequence[Position]:
        """Used by the safety checklist's "position synced" check."""
        ...

    def submit_order(self, order: Order, *, client_order_id: ClientOrderId) -> None:
        """Fire-and-forget order submission. The broker's actual accept/reject arrives
        later via a published `OrderReportReceived` — never inferred from this call
        returning without raising. Only ever called by `OrderManager.submit`, after the
        corresponding `OrderIntent` has already been durably persisted."""
        ...

    def cancel_order(self, client_order_id: ClientOrderId) -> None:
        """Fire-and-forget cancel request for an already-acknowledged or
        partially-filled order. The confirmation (or a fill that races it) arrives later
        via the normal report/fill events."""
        ...

    def query_order_reports(self) -> Sequence[OrderReport]:
        """The broker's current queryable order-report set, for startup/reconnect
        reconciliation — see `OrderManager.reconcile_on_startup`."""
        ...

    def query_fills(self) -> Sequence[Fill]:
        """The broker's current queryable fill set, same reconciliation purpose as
        `query_order_reports`."""
        ...
