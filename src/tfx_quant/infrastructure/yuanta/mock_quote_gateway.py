"""A mock QuoteGatewayPort — no COM/network calls, used by default (`use_mock: true`)
so the whole codebase builds and tests without the vendor API installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument


@dataclass(frozen=True, slots=True)
class SubscriptionRecord:
    """A recorded subscribe() call, exposed for test assertions."""

    instrument: Instrument
    contract: ContractMonth


class MockQuoteGateway:
    """Implements `application.ports.yuanta_gateways.QuoteGatewayPort`."""

    def __init__(self, *, market_data_valid: bool = False) -> None:
        self._market_data_valid = market_data_valid
        self.subscriptions: list[SubscriptionRecord] = []
        self.unsubscriptions: list[SubscriptionRecord] = []

    def is_market_data_valid(self) -> bool:
        return self._market_data_valid

    def subscribe(self, instrument: Instrument, contract: ContractMonth) -> None:
        self.subscriptions.append(SubscriptionRecord(instrument, contract))

    def unsubscribe(self, instrument: Instrument, contract: ContractMonth) -> None:
        record = SubscriptionRecord(instrument, contract)
        if record in self.subscriptions:
            self.subscriptions.remove(record)
        self.unsubscriptions.append(record)

    def set_market_data_valid(self, value: bool) -> None:
        self._market_data_valid = value
