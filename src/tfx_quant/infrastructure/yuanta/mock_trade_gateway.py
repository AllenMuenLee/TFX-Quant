"""A mock TradeGatewayPort — no COM/network calls, used by default (`use_mock: true`)
so the whole codebase builds and tests without the vendor API installed.

`submit_order`/`cancel_order` never publish anything on their own — tests opt into a
specific scenario by calling the `simulate_*` methods explicitly (including from an
`on_submit` callback invoked synchronously *inside* `submit_order`, before it returns,
for the "回報先於函式返回" acceptance scenario), same scripted-outcome style as
`MockBrokerSession`.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from decimal import Decimal

from tfx_quant.application.events.events import Event, FillReceived, OrderReportReceived
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.event_publisher import EventPublisher

OnSubmit = Callable[["MockTradeGateway", Order, ClientOrderId], None]


class _NullEventPublisher:
    def publish(self, event: Event) -> None:
        pass


class MockTradeGateway:
    """Implements `application.ports.yuanta_gateways.TradeGatewayPort`."""

    def __init__(
        self,
        *,
        logged_in: bool = False,
        open_orders: Sequence[Order] = (),
        positions: Sequence[Position] = (),
        event_publisher: EventPublisher | None = None,
        on_submit: OnSubmit | None = None,
        track_positions_from_fills: bool = False,
    ) -> None:
        self._logged_in = logged_in
        self._open_orders = list(open_orders)
        self._positions = list(positions)
        self._event_publisher: EventPublisher = event_publisher or _NullEventPublisher()
        self._on_submit = on_submit
        self._track_positions_from_fills = track_positions_from_fills
        self._position_fill_ids: set[str] = set()
        self._seq_counter = itertools.count(1)
        self._query_positions_error: Exception | None = None

        self.submitted_orders: list[Order] = []
        """Every `submit_order` call, in order — the "no second duplicate order" test
        assertion is `len(gateway.submitted_orders) == 1`."""
        self.cancelled_order_ids: list[ClientOrderId] = []
        self._orders_by_client_id: dict[ClientOrderId, Order] = {}
        self._order_reports: list[OrderReport] = []
        self._fills: list[Fill] = []
        self._last_fill: Fill | None = None

    # -- TradeGatewayPort -----------------------------------------------------------

    def is_logged_in(self) -> bool:
        return self._logged_in

    def query_open_orders(self) -> Sequence[Order]:
        return tuple(self._open_orders)

    def query_positions(self) -> Sequence[Position]:
        if self._query_positions_error is not None:
            error, self._query_positions_error = self._query_positions_error, None
            raise error
        return tuple(self._positions)

    def submit_order(self, order: Order, *, client_order_id: ClientOrderId) -> None:
        self.submitted_orders.append(order)
        self._orders_by_client_id[client_order_id] = order
        if self._on_submit is not None:
            self._on_submit(self, order, client_order_id)

    def cancel_order(self, client_order_id: ClientOrderId) -> None:
        self.cancelled_order_ids.append(client_order_id)

    def query_order_reports(self) -> Sequence[OrderReport]:
        return tuple(self._order_reports)

    def query_fills(self) -> Sequence[Fill]:
        return tuple(self._fills)

    # -- Test setup -------------------------------------------------------------------

    def set_logged_in(self, value: bool) -> None:
        self._logged_in = value

    def add_position(self, position: Position) -> None:
        self._positions.append(position)

    def set_positions(self, positions: Sequence[Position]) -> None:
        """Replaces the full scripted `query_positions()` result — used by tests that
        need to simulate the broker's position query changing over time (e.g. a
        position becoming flat only after the test explicitly updates it, independent
        of any fill simulated via `simulate_fill`, since this mock does no fill-to-
        position accounting of its own)."""
        self._positions = list(positions)

    def add_open_order(self, order: Order) -> None:
        self._open_orders.append(order)

    def fail_next_query_positions(self, error: Exception) -> None:
        """Scripts `query_positions()` to raise `error` exactly once, then resume
        returning whatever `set_positions()`/`add_position()` have scripted — the
        "查詢暫時失敗" acceptance scenario (Feature 08)."""
        self._query_positions_error = error

    def set_order_reports(self, reports: Sequence[OrderReport]) -> None:
        """Scripts what `query_order_reports()` (the reconciliation query) returns."""
        self._order_reports = list(reports)

    def set_fills(self, fills: Sequence[Fill]) -> None:
        """Scripts what `query_fills()` (the reconciliation query) returns."""
        self._fills = list(fills)

    # -- Scripting: order reports ----------------------------------------------------

    def simulate_ack(
        self,
        client_order_id: ClientOrderId,
        broker_order_no: str,
        *,
        seq: int | None = None,
        at: Timestamp | None = None,
    ) -> OrderReport:
        return self._publish_report(
            client_order_id,
            status=OrderStatus.ACKNOWLEDGED,
            broker_order_no=broker_order_no,
            seq=seq,
            at=at,
        )

    def simulate_reject(
        self, client_order_id: ClientOrderId, reason: str, *, seq: int | None = None
    ) -> OrderReport:
        return self._publish_report(
            client_order_id, status=OrderStatus.REJECTED, reject_reason=reason, seq=seq
        )

    def simulate_cancel_confirmed(
        self, client_order_id: ClientOrderId, *, seq: int | None = None
    ) -> OrderReport:
        return self._publish_report(client_order_id, status=OrderStatus.CANCELLED, seq=seq)

    def _publish_report(
        self,
        client_order_id: ClientOrderId,
        *,
        status: OrderStatus,
        broker_order_no: str | None = None,
        reject_reason: str | None = None,
        seq: int | None = None,
        at: Timestamp | None = None,
    ) -> OrderReport:
        report = OrderReport(
            client_order_id=client_order_id,
            status=status,
            broker_seq_no=seq if seq is not None else next(self._seq_counter),
            at=at or Timestamp.now(),
            broker_order_no=broker_order_no,
            reject_reason=reject_reason,
        )
        self._order_reports.append(report)
        self._event_publisher.publish(OrderReportReceived(at=report.at, report=report))
        return report

    # -- Scripting: fills ---------------------------------------------------------------

    def simulate_fill(
        self,
        client_order_id: ClientOrderId,
        quantity: int,
        price: Decimal,
        *,
        broker_fill_no: str,
        seq: int | None = None,
        at: Timestamp | None = None,
    ) -> Fill:
        order = self._orders_by_client_id.get(client_order_id)
        instrument = order.instrument if order is not None else Instrument.MXF
        side = order.side if order is not None else Side.BUY
        fill = Fill(
            client_order_id=client_order_id,
            instrument=instrument,
            side=side,
            quantity=Quantity(quantity),
            price=Price(price),
            at=at or Timestamp.now(),
            broker_fill_no=broker_fill_no,
            broker_seq_no=seq if seq is not None else next(self._seq_counter),
        )
        self._fills.append(fill)
        self._last_fill = fill
        if self._track_positions_from_fills and broker_fill_no not in self._position_fill_ids:
            self._apply_fill_to_positions(order, fill)
            self._position_fill_ids.add(broker_fill_no)
        self._event_publisher.publish(FillReceived(at=fill.at, fill=fill))
        return fill

    def _apply_fill_to_positions(self, order: Order | None, fill: Fill) -> None:
        if order is None:
            return
        match_index = next(
            (
                index
                for index, position in enumerate(self._positions)
                if position.account == order.account
                and position.instrument == order.instrument
                and position.contract == order.contract
            ),
            None,
        )
        existing = self._positions[match_index] if match_index is not None else None
        prior = 0 if existing is None else existing.net.lots
        delta = fill.quantity.lots if fill.side is Side.BUY else -fill.quantity.lots
        updated = prior + delta
        if updated == 0:
            if match_index is not None:
                del self._positions[match_index]
            return
        if existing is None or prior == 0 or (prior > 0) != (updated > 0):
            average = fill.price
        elif (prior > 0) == (delta > 0):
            total = abs(prior) + abs(delta)
            average = Price(
                (existing.average_price.amount * abs(prior) + fill.price.amount * abs(delta))
                / total
            )
        else:
            assert existing.average_price is not None
            average = existing.average_price
        position = Position(
            account=order.account,
            instrument=order.instrument,
            contract=order.contract,
            net=NetPosition(updated),
            average_price=average,
            as_of=fill.at,
        )
        if match_index is None:
            self._positions.append(position)
        else:
            self._positions[match_index] = position

    def replay_last_fill(self) -> Fill:
        """Re-publishes the exact last `FillReceived` — the duplicate-fill acceptance
        scenario."""
        if self._last_fill is None:
            raise ValueError("no fill has been simulated yet")
        self._event_publisher.publish(FillReceived(at=Timestamp.now(), fill=self._last_fill))
        return self._last_fill
