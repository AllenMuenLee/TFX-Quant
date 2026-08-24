"""Thin `TradeGatewayPort` view over a real `IBrokerSession`.

Feature 01's narrow port (`is_logged_in`, `query_open_orders`, `query_positions`)
predates `IBrokerSession` and stays as the surface other features query. For the mock
branch, `MockBrokerSession` doesn't need this — `MockTradeGateway` is used directly (see
`composition.py`). There is no quote-gateway view here — market data comes entirely from
`yfinance`, never this session.

`query_open_orders`/`query_positions` still raise rather than fabricating an "always
empty" answer: SPARK API's `GetRealReport`/`GetFutStoreSummary` results aren't parsed
into typed `Order`/`Position` objects yet (see `session_orchestrator.py`'s docstrings
on `handle_real_report_query_result`/`handle_position_query_result` — building typed
objects needs an idempotency-key mapping that's Feature 06/08's job). Returning `()`
here instead of raising would silently look like "confirmed no open orders" to
`StartupSafetyGate`'s "no unknown orders" check — a false safety signal — so these
still raise.

`submit_order`/`cancel_order`/`query_order_reports`/`query_fills` (Feature 06) raise the
same way, for the same reason: this codebase's "不得臆測：API 名稱、參數..." rule means
real 委託/回報 SPARK API method names may only be wired in once someone with real
credentials has read the live docs (see `implementation prompt/06-order-and-fill-state-
machine/implementation-prompt.md`'s banner). `application.order_management.OrderManager`
and its tests are fully built against `MockTradeGateway` instead — see
`docs/adr/0008-order-and-fill-state-machine.md`.
"""

from __future__ import annotations

from collections.abc import Sequence

from tfx_quant.application.ports.broker_session import IBrokerSession
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.order import ClientOrderId, Order
from tfx_quant.domain.order_state_machine import OrderReport
from tfx_quant.domain.position import Position

_NOT_YET_PARSED_MESSAGE = (
    "尚未實作將元大查詢結果解析為型別化物件（見 "
    "infrastructure/yuanta/broker_session_gateway_views.py）；"
    "委託/成交/持倉的結構化查詢預計於後續 feature 完成。"
)


class BrokerSessionTradeGatewayView:
    """Implements `TradeGatewayPort` — delegates the boolean to the real session."""

    def __init__(self, broker_session: IBrokerSession) -> None:
        self._broker_session = broker_session

    def is_logged_in(self) -> bool:
        return self._broker_session.capabilities.login

    def query_open_orders(self) -> Sequence[Order]:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)

    def query_positions(self) -> Sequence[Position]:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)

    def submit_order(self, order: Order, *, client_order_id: ClientOrderId) -> None:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)

    def cancel_order(self, client_order_id: ClientOrderId) -> None:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)

    def query_order_reports(self) -> Sequence[OrderReport]:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)

    def query_fills(self) -> Sequence[Fill]:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)
