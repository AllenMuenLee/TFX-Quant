"""Yuanta broker gateway ports.

Feature 01 only defines the readiness/query surface needed by the
`StartupSafetyGate` checklist (login state, order/position sync). Order submission
(`SendOrderF` and friends) is deliberately out of scope until Feature 06 — no code
path to send an order exists anywhere in this codebase yet.

Only `infrastructure.yuanta` may ever import the vendor COM/OCX types; everything
else, including the desktop composition root, depends on these Protocols.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import Order
from tfx_quant.domain.position import Position


class TradeGatewayPort(Protocol):
    def is_logged_in(self) -> bool: ...

    def query_open_orders(self) -> Sequence[Order]:
        """Used by the safety checklist's "no unknown orders" check."""
        ...

    def query_positions(self) -> Sequence[Position]:
        """Used by the safety checklist's "position synced" check."""
        ...


class QuoteGatewayPort(Protocol):
    def is_market_data_valid(self) -> bool: ...

    def subscribe(self, instrument: Instrument, contract: ContractMonth) -> None: ...

    def unsubscribe(self, instrument: Instrument, contract: ContractMonth) -> None:
        """Cancel a previous `subscribe()` — used by Feature 03's instrument/contract
        switch flow to drop the old contract's quote registration before subscribing
        to the new one. Safe to call for a contract that was never subscribed."""
        ...
