"""Connectivity-tracking wrappers — observe existing `TradeGatewayPort`/`IBrokerSession`
calls other services already make, feeding `ConnectivityMonitor` (via the `QueryObserver`
seam it implements) without making a single additional broker call of their own.

Neither wrapper ever swallows an exception: every observed call is re-raised exactly as
the wrapped implementation raised it, so `OrderManager`/`PositionReconciliationService`/
`ReversalWorkflowService`/`ScalingService`'s own error handling (mark UNKNOWN, log and
continue, etc.) is completely unaffected by wrapping — the wrapper only ever *observes*.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

from tfx_quant.application.ports.broker_session import (
    IBrokerSession,
    LoginRequest,
    SessionCapabilities,
)
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.order import ClientOrderId, Order
from tfx_quant.domain.order_state_machine import OrderReport
from tfx_quant.domain.position import Position

_T = TypeVar("_T")


class QueryObserver(Protocol):
    """The seam `ConnectivityMonitor` implements — kept as a narrow Protocol here
    (rather than importing the concrete class) so this module never depends on the
    monitor's full constructor/event-bus wiring, only the handful of methods it needs
    to call into."""

    def record_query_result(
        self,
        *,
        call: str,
        ok: bool,
        latency_ms: float,
        error: str | None,
        positions: Sequence[Position] = (),
    ) -> None: ...

    def record_trade_result(
        self, *, call: str, ok: bool, latency_ms: float, error: str | None
    ) -> None: ...

    def remember_login_request(self, request: LoginRequest) -> None: ...

    def forget_login_request(self) -> None: ...


class ConnectivityTrackingTradeGateway:
    """Implements `TradeGatewayPort`. Every query/submit/cancel call is timed and its
    outcome reported to `observer`."""

    def __init__(self, inner: TradeGatewayPort, observer: QueryObserver) -> None:
        self._inner = inner
        self._observer = observer

    def is_logged_in(self) -> bool:
        return self._inner.is_logged_in()

    def query_open_orders(self) -> Sequence[Order]:
        return self._timed_query("query_open_orders", self._inner.query_open_orders)

    def query_positions(self) -> Sequence[Position]:
        return self._timed_query(
            "query_positions", self._inner.query_positions, report_positions=True
        )

    def submit_order(self, order: Order, *, client_order_id: ClientOrderId) -> None:
        self._timed_trade(
            "submit_order",
            lambda: self._inner.submit_order(order, client_order_id=client_order_id),
        )

    def cancel_order(self, client_order_id: ClientOrderId) -> None:
        self._timed_trade("cancel_order", lambda: self._inner.cancel_order(client_order_id))

    def query_order_reports(self) -> Sequence[OrderReport]:
        return self._timed_query("query_order_reports", self._inner.query_order_reports)

    def query_fills(self) -> Sequence[Fill]:
        return self._timed_query("query_fills", self._inner.query_fills)

    def _timed_query(
        self, name: str, fn: Callable[[], _T], *, report_positions: bool = False
    ) -> _T:
        start = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - observed, then always re-raised
            self._observer.record_query_result(
                call=name,
                ok=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise
        self._observer.record_query_result(
            call=name,
            ok=True,
            latency_ms=(time.monotonic() - start) * 1000,
            error=None,
            positions=result if report_positions else (),  # type: ignore[arg-type]
        )
        return result

    def _timed_trade(self, name: str, fn: Callable[[], None]) -> None:
        start = time.monotonic()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - observed, then always re-raised
            self._observer.record_trade_result(
                call=name,
                ok=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )
            raise
        self._observer.record_trade_result(
            call=name, ok=True, latency_ms=(time.monotonic() - start) * 1000, error=None
        )


class ConnectivityTrackingBrokerSession:
    """Implements `IBrokerSession` — delegates every call, and additionally remembers
    the most recent `start()` request so `ConnectivityMonitor` can retry it after a
    passive disconnect without needing the operator to re-enter credentials. `start()`
    is otherwise only ever called from UI code (`desktop/login_dialog.py`) that has the
    request in hand; nothing else in this codebase keeps it around."""

    def __init__(self, inner: IBrokerSession, observer: QueryObserver) -> None:
        self._inner = inner
        self._observer = observer

    @property
    def capabilities(self) -> SessionCapabilities:
        return self._inner.capabilities

    @property
    def accounts(self) -> Sequence[TradingAccount]:
        return self._inner.accounts

    @property
    def selected_account(self) -> TradingAccount | None:
        return self._inner.selected_account

    def start(self, request: LoginRequest) -> None:
        self._observer.remember_login_request(request)
        self._inner.start(request)

    def select_account(self, account: TradingAccount) -> None:
        self._inner.select_account(account)

    def cancel_start(self) -> None:
        self._inner.cancel_start()

    def subscribe_market_data(self, symbol: str) -> None:
        self._inner.subscribe_market_data(symbol)

    def unsubscribe_market_data(self, symbol: str) -> None:
        self._inner.unsubscribe_market_data(symbol)

    def stop(self) -> None:
        # An explicit disconnect — never auto-reconnect after this (see
        # `ConnectivityMonitor.forget_login_request`).
        self._observer.forget_login_request()
        self._inner.stop()


__all__ = ["ConnectivityTrackingBrokerSession", "ConnectivityTrackingTradeGateway", "QueryObserver"]
