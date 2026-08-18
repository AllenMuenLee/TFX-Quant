from __future__ import annotations

from tfx_quant.application.ports.yuanta_gateways import QuoteGatewayPort, TradeGatewayPort
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.infrastructure.yuanta.mock_quote_gateway import (
    MockQuoteGateway,
    SubscriptionRecord,
)
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway


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
