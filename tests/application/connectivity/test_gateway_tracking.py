from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from pydantic import SecretStr

from tfx_quant.application.connectivity.gateway_tracking import (
    ConnectivityTrackingBrokerSession,
    ConnectivityTrackingTradeGateway,
)
from tfx_quant.application.ports.broker_session import LoginRequest, SessionCapabilities
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Price(Decimal("18500"))


@dataclass
class _RecordedQuery:
    call: str
    ok: bool
    latency_ms: float
    error: str | None
    positions: Sequence[Position] = field(default_factory=tuple)


@dataclass
class _RecordedTrade:
    call: str
    ok: bool
    latency_ms: float
    error: str | None


class _RecordingObserver:
    def __init__(self) -> None:
        self.queries: list[_RecordedQuery] = []
        self.trades: list[_RecordedTrade] = []
        self.remembered_requests: list[LoginRequest] = []
        self.forgotten = 0

    def record_query_result(
        self,
        *,
        call: str,
        ok: bool,
        latency_ms: float,
        error: str | None,
        positions: Sequence[Position] = (),
    ) -> None:
        self.queries.append(_RecordedQuery(call, ok, latency_ms, error, positions))

    def record_trade_result(
        self, *, call: str, ok: bool, latency_ms: float, error: str | None
    ) -> None:
        self.trades.append(_RecordedTrade(call, ok, latency_ms, error))

    def remember_login_request(self, request: LoginRequest) -> None:
        self.remembered_requests.append(request)

    def forget_login_request(self) -> None:
        self.forgotten += 1


def _order() -> Order:
    return Order(
        client_order_id=ClientOrderId(),
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=Side.BUY,
        quantity=Quantity(1),
        price=_PRICE,
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
    )


def _position(net: int) -> Position:
    return Position(
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        net=NetPosition(net),
        average_price=_PRICE if net != 0 else None,
        as_of=Timestamp.now(),
    )


# -- ConnectivityTrackingTradeGateway -----------------------------------------------------


def test_successful_query_positions_is_observed_with_the_returned_positions() -> None:
    observer = _RecordingObserver()
    position = _position(2)
    inner = MockTradeGateway(positions=[position])
    gateway = ConnectivityTrackingTradeGateway(inner, observer)

    result = gateway.query_positions()

    assert result == (position,)
    assert len(observer.queries) == 1
    recorded = observer.queries[0]
    assert recorded.call == "query_positions"
    assert recorded.ok is True
    assert recorded.error is None
    assert recorded.positions == (position,)


def test_failed_query_positions_is_observed_and_the_exception_still_propagates() -> None:
    observer = _RecordingObserver()
    inner = MockTradeGateway()
    inner.fail_next_query_positions(RuntimeError("timeout"))
    gateway = ConnectivityTrackingTradeGateway(inner, observer)

    with pytest.raises(RuntimeError, match="timeout"):
        gateway.query_positions()

    assert len(observer.queries) == 1
    recorded = observer.queries[0]
    assert recorded.ok is False
    assert recorded.error == "timeout"
    assert recorded.positions == ()


def test_query_order_reports_and_query_fills_are_observed_without_positions() -> None:
    observer = _RecordingObserver()
    inner = MockTradeGateway()
    gateway = ConnectivityTrackingTradeGateway(inner, observer)

    gateway.query_order_reports()
    gateway.query_fills()

    calls = [q.call for q in observer.queries]
    assert calls == ["query_order_reports", "query_fills"]
    assert all(q.positions == () for q in observer.queries)


def test_submit_order_success_is_observed_as_a_trade_result() -> None:
    observer = _RecordingObserver()
    inner = MockTradeGateway()
    gateway = ConnectivityTrackingTradeGateway(inner, observer)
    order = _order()

    gateway.submit_order(order, client_order_id=order.client_order_id)

    assert inner.submitted_orders == [order]
    assert len(observer.trades) == 1
    recorded = observer.trades[0]
    assert recorded.call == "submit_order"
    assert recorded.ok is True
    assert recorded.error is None


def test_cancel_order_failure_is_observed_and_reraised() -> None:
    observer = _RecordingObserver()

    class _RaisingGateway(MockTradeGateway):
        def cancel_order(self, client_order_id: ClientOrderId) -> None:
            raise ValueError("no such order")

    gateway = ConnectivityTrackingTradeGateway(_RaisingGateway(), observer)

    with pytest.raises(ValueError, match="no such order"):
        gateway.cancel_order(ClientOrderId())

    assert len(observer.trades) == 1
    assert observer.trades[0].ok is False
    assert observer.trades[0].error == "no such order"


def test_is_logged_in_passes_through_without_being_observed() -> None:
    observer = _RecordingObserver()
    inner = MockTradeGateway(logged_in=True)
    gateway = ConnectivityTrackingTradeGateway(inner, observer)

    assert gateway.is_logged_in() is True
    assert observer.queries == []
    assert observer.trades == []


def test_query_open_orders_delegates_and_is_observed() -> None:
    observer = _RecordingObserver()
    order = _order()
    inner = MockTradeGateway(open_orders=[order])
    gateway = ConnectivityTrackingTradeGateway(inner, observer)

    result = gateway.query_open_orders()

    assert result == (order,)
    assert observer.queries[0].call == "query_open_orders"


def test_query_fills_returns_the_underlying_fill_sequence() -> None:
    observer = _RecordingObserver()
    fill = Fill(
        client_order_id=ClientOrderId(),
        instrument=Instrument.MXF,
        side=Side.BUY,
        quantity=Quantity(1),
        price=_PRICE,
        at=Timestamp.now(),
        broker_fill_no="F-1",
        broker_seq_no=1,
    )
    inner = MockTradeGateway()
    inner.set_fills([fill])
    gateway = ConnectivityTrackingTradeGateway(inner, observer)

    assert gateway.query_fills() == (fill,)


# -- ConnectivityTrackingBrokerSession ------------------------------------------------------


def _login_request() -> LoginRequest:
    return LoginRequest(
        environment=Environment.TEST, user_id="F00000000012345678", password=SecretStr("x")
    )


def test_start_remembers_the_login_request_and_delegates() -> None:
    observer = _RecordingObserver()
    inner = MockBrokerSession()
    session = ConnectivityTrackingBrokerSession(inner, observer)
    request = _login_request()

    session.start(request)

    assert observer.remembered_requests == [request]
    assert session.capabilities.is_session_ready is True  # delegated to the mock happy path


def test_stop_forgets_the_login_request_and_delegates() -> None:
    observer = _RecordingObserver()
    inner = MockBrokerSession()
    session = ConnectivityTrackingBrokerSession(inner, observer)
    session.start(_login_request())

    session.stop()

    assert observer.forgotten == 1
    assert session.capabilities == SessionCapabilities()


def test_select_account_and_market_data_subscription_delegate_untouched() -> None:
    observer = _RecordingObserver()
    inner = MockBrokerSession()
    session = ConnectivityTrackingBrokerSession(inner, observer)
    session.start(_login_request())

    session.subscribe_market_data("TXFH6")
    assert "TXFH6" in inner.subscribed_symbols
    session.unsubscribe_market_data("TXFH6")
    assert "TXFH6" not in inner.subscribed_symbols
