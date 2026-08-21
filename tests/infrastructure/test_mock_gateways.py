from __future__ import annotations

from decimal import Decimal

import pytest

from tfx_quant.application.events.events import Event, FillReceived, OrderReportReceived
from tfx_quant.application.ports.yuanta_gateways import QuoteGatewayPort, TradeGatewayPort
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.infrastructure.yuanta.mock_quote_gateway import (
    MockQuoteGateway,
    SubscriptionRecord,
)
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish(self, event: Event) -> None:
        self.published.append(event)


def _order(client_order_id: ClientOrderId) -> Order:
    return Order(
        client_order_id=client_order_id,
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("18500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
    )


def test_mock_trade_gateway_satisfies_the_port() -> None:
    gateway: TradeGatewayPort = MockTradeGateway()
    assert gateway.is_logged_in() is False
    assert gateway.query_open_orders() == ()
    assert gateway.query_positions() == ()


def test_mock_trade_gateway_set_logged_in() -> None:
    gateway = MockTradeGateway()
    gateway.set_logged_in(True)
    assert gateway.is_logged_in() is True


def test_mock_quote_gateway_satisfies_the_port() -> None:
    gateway: QuoteGatewayPort = MockQuoteGateway()
    assert gateway.is_market_data_valid() is False
    gateway.subscribe(Instrument.MXF, ContractMonth(year=2026, month=9))


def test_mock_quote_gateway_records_subscriptions() -> None:
    gateway = MockQuoteGateway(market_data_valid=True)
    contract = ContractMonth(year=2026, month=9)
    gateway.subscribe(Instrument.MXF, contract)
    assert gateway.subscriptions == [
        SubscriptionRecord(instrument=Instrument.MXF, contract=contract)
    ]
    assert gateway.is_market_data_valid() is True


def test_mock_quote_gateway_unsubscribe_removes_from_subscriptions() -> None:
    gateway = MockQuoteGateway()
    contract = ContractMonth(year=2026, month=9)
    gateway.subscribe(Instrument.MXF, contract)

    gateway.unsubscribe(Instrument.MXF, contract)

    assert gateway.subscriptions == []
    assert gateway.unsubscriptions == [
        SubscriptionRecord(instrument=Instrument.MXF, contract=contract)
    ]


def test_mock_quote_gateway_unsubscribe_without_prior_subscribe_is_safe() -> None:
    gateway = MockQuoteGateway()
    contract = ContractMonth(year=2026, month=9)
    gateway.unsubscribe(Instrument.MXF, contract)
    assert gateway.unsubscriptions == [
        SubscriptionRecord(instrument=Instrument.MXF, contract=contract)
    ]


# -- MockTradeGateway: order submission (Feature 06) -----------------------------------


def test_mock_trade_gateway_submit_order_records_call() -> None:
    gateway = MockTradeGateway()
    client_order_id = ClientOrderId()
    gateway.submit_order(_order(client_order_id), client_order_id=client_order_id)
    assert len(gateway.submitted_orders) == 1
    assert gateway.submitted_orders[0].client_order_id == client_order_id


def test_mock_trade_gateway_cancel_order_records_call() -> None:
    gateway = MockTradeGateway()
    client_order_id = ClientOrderId()
    gateway.cancel_order(client_order_id)
    assert gateway.cancelled_order_ids == [client_order_id]


def test_mock_trade_gateway_simulate_ack_publishes_order_report_received() -> None:
    publisher = _RecordingPublisher()
    gateway = MockTradeGateway(event_publisher=publisher)
    client_order_id = ClientOrderId()
    gateway.submit_order(_order(client_order_id), client_order_id=client_order_id)

    gateway.simulate_ack(client_order_id, "B0001")

    assert len(publisher.published) == 1
    event = publisher.published[0]
    assert isinstance(event, OrderReportReceived)
    assert event.report.status is OrderStatus.ACKNOWLEDGED
    assert event.report.broker_order_no == "B0001"


def test_mock_trade_gateway_simulate_fill_uses_submitted_orders_instrument_and_side() -> None:
    publisher = _RecordingPublisher()
    gateway = MockTradeGateway(event_publisher=publisher)
    client_order_id = ClientOrderId()
    gateway.submit_order(_order(client_order_id), client_order_id=client_order_id)

    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")

    assert len(publisher.published) == 1
    event = publisher.published[0]
    assert isinstance(event, FillReceived)
    assert event.fill.instrument is Instrument.MXF
    assert event.fill.side is Side.BUY
    assert event.fill.broker_fill_no == "F1"


def test_mock_trade_gateway_query_order_reports_and_fills_return_scripted_values() -> None:
    gateway = MockTradeGateway()
    client_order_id = ClientOrderId()
    gateway.submit_order(_order(client_order_id), client_order_id=client_order_id)
    gateway.simulate_ack(client_order_id, "B0001")
    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")

    assert len(gateway.query_order_reports()) == 1
    assert len(gateway.query_fills()) == 1


def test_mock_trade_gateway_on_submit_callback_fires_before_submit_order_returns() -> None:
    """The "回報先於函式返回" acceptance scenario: the ack is published synchronously
    from inside `submit_order`, before it returns to the caller."""
    publisher = _RecordingPublisher()
    seen_before_return: list[bool] = []

    def on_submit(gw: MockTradeGateway, order: Order, client_order_id: ClientOrderId) -> None:
        gw.simulate_ack(client_order_id, "B0001")
        seen_before_return.append(len(publisher.published) == 1)

    gateway = MockTradeGateway(event_publisher=publisher, on_submit=on_submit)
    client_order_id = ClientOrderId()
    gateway.submit_order(_order(client_order_id), client_order_id=client_order_id)

    assert seen_before_return == [True]


def test_mock_trade_gateway_fail_next_query_positions_raises_once_then_recovers() -> None:
    """The "查詢暫時失敗" acceptance scenario (Feature 08)."""
    gateway = MockTradeGateway()
    gateway.fail_next_query_positions(ConnectionError("transient"))

    with pytest.raises(ConnectionError):
        gateway.query_positions()

    assert gateway.query_positions() == ()


def test_mock_trade_gateway_replay_last_fill_republishes_same_fill() -> None:
    publisher = _RecordingPublisher()
    gateway = MockTradeGateway(event_publisher=publisher)
    client_order_id = ClientOrderId()
    gateway.submit_order(_order(client_order_id), client_order_id=client_order_id)
    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1", seq=5)

    gateway.replay_last_fill()

    assert len(publisher.published) == 2
    first = publisher.published[0]
    second = publisher.published[1]
    assert isinstance(first, FillReceived)
    assert isinstance(second, FillReceived)
    assert first.fill.broker_fill_no == second.fill.broker_fill_no == "F1"
    assert first.fill.broker_seq_no == second.fill.broker_seq_no == 5
