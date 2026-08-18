"""A mock TradeGatewayPort — no COM/network calls, used by default (`use_mock: true`)
so the whole codebase builds and tests without the vendor API installed.
"""

from __future__ import annotations

from collections.abc import Sequence

from tfx_quant.domain.order import Order
from tfx_quant.domain.position import Position


class MockTradeGateway:
    """Implements `application.ports.yuanta_gateways.TradeGatewayPort`."""

    def __init__(
        self,
        *,
        logged_in: bool = False,
        open_orders: Sequence[Order] = (),
        positions: Sequence[Position] = (),
    ) -> None:
        self._logged_in = logged_in
        self._open_orders = list(open_orders)
        self._positions = list(positions)

    def is_logged_in(self) -> bool:
        return self._logged_in

    def query_open_orders(self) -> Sequence[Order]:
        return tuple(self._open_orders)

    def query_positions(self) -> Sequence[Position]:
        return tuple(self._positions)

    def set_logged_in(self, value: bool) -> None:
        self._logged_in = value

    def add_position(self, position: Position) -> None:
        self._positions.append(position)

    def add_open_order(self, order: Order) -> None:
        self._open_orders.append(order)
