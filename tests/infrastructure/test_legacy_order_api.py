from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.infrastructure.yuanta.legacy_broker import LegacyBroker
from tfx_quant.infrastructure.yuanta.legacy_order_api import (
    LegacyOrderApiClient,
    LegacyOrderApiError,
    broker_fill_key,
    make_fill,
    order_report_status,
    parse_order_result,
    parse_pipe_fields,
)


class FakeControl:
    def __init__(self) -> None:
        self.wait_flags: list[int] = []
        self.calls: list[tuple[str, ...]] = []
        self.connections: list[tuple[str, str, str, int]] = []

    def SetWaitOrdResult(self, flag: int) -> None:
        self.wait_flags.append(flag)

    def SendOrderF(self, *args: str) -> str:
        self.calls.append(args)
        return "REQ-1"

    def ReportQuery(self, *args: str) -> int:
        return 2

    def DealQuery(self, *args: str) -> int:
        return 2

    def UserDefinsFunc(self, _params: str, _work_id: str) -> int:
        return 2

    def SetFutOrdConnection(self, user: str, password: str, host: str, port: int) -> int:
        self.connections.append((user, password, host, port))
        return 0


class FakeHost:
    def __init__(self) -> None:
        self.control = FakeControl()
        self.handlers: dict[str, Any] = {}

    def bind(self, event_name: str, handler: Any) -> None:
        self.handlers[event_name] = handler

    def close(self) -> None:
        pass


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[object] = []

    def publish(self, event: object) -> None:
        self.events.append(event)


def _order() -> Order:
    return Order(
        client_order_id=ClientOrderId(UUID(int=1)),
        account=TradingAccount(branch_id="F00", account_no="1234567", sub_account=""),
        instrument=Instrument.MXF,
        contract=ContractMonth(2026, 9),
        side=Side.BUY,
        quantity=Quantity(2),
        price=Price(Decimal("18500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
    )


def test_submit_uses_documented_send_order_f_fields_and_async_mode() -> None:
    control = FakeControl()
    client = LegacyOrderApiClient(control, lambda _order: "MXFU6")
    order = _order()

    request_id = client.submit_order(order, order.client_order_id)

    assert request_id == "REQ-1"
    assert control.wait_flags == [0]
    assert control.calls == [
        (
            "01",
            "0",
            "F00",
            "1234567",
            "",
            "",
            "B",
            "MXFU6",
            "18500",
            "2",
            "0",
            "L",
            "R",
            "",
            "",
        )
    ]


def test_order_result_links_documented_request_id_to_oseq_no() -> None:
    control = FakeControl()
    client = LegacyOrderApiClient(control, lambda _order: "MXFU6")
    order = _order()
    client.submit_order(order, order.client_order_id)

    result = client.handle_order_result("REQ-1", "202608210001|0000|")
    correlated = client.correlate({"Oseq_No": "202608210001", "Order_No": "A1234"})

    assert result.order_sequence_no == "202608210001"
    assert correlated == order.client_order_id


def test_order_result_can_arrive_synchronously_before_send_returns() -> None:
    control = FakeControl()
    client = LegacyOrderApiClient(control, lambda _order: "MXFU6")

    def synchronous_send(*args: str) -> str:
        control.calls.append(args)
        client.handle_order_result("7", "SEQ-SYNC|0000|")
        return "7"

    control.SendOrderF = synchronous_send  # type: ignore[method-assign]
    order = _order()
    client.submit_order(order, order.client_order_id)

    assert client.correlate({"Oseq_No": "SEQ-SYNC"}) == order.client_order_id


@pytest.mark.parametrize(
    ("statusc", "ts_code", "expected"),
    [
        ("1", "00", OrderStatus.SUBMITTING),
        ("0", "04", OrderStatus.ACKNOWLEDGED),
        ("0", "06", OrderStatus.FILLED),
        ("0", "07", OrderStatus.CANCELLED),
        ("0", "08", OrderStatus.PARTIALLY_FILLED),
        ("0", "09", OrderStatus.CANCELLED),
        ("2", "05", OrderStatus.REJECTED),
        ("0", "99", OrderStatus.UNKNOWN),
    ],
)
def test_documented_report_status_mapping(
    statusc: str, ts_code: str, expected: OrderStatus
) -> None:
    assert order_report_status({"Statusc": statusc, "Ts_Code": ts_code}) is expected


def test_fill_dedup_key_is_stable_and_fill_is_typed() -> None:
    fields = {
        "Oseq_No": "SEQ1",
        "Order_No": "A1234",
        "D_Time": "091031",
        "Deal_Qty": "1",
        "A_Prc": "18500",
    }
    client_id = ClientOrderId(UUID(int=1))

    first_key = broker_fill_key(fields)
    fill = make_fill(fields, client_id, Instrument.MXF, Side.BUY, 1)

    assert broker_fill_key(dict(fields)) == first_key
    assert fill.broker_fill_no == first_key
    assert fill.quantity == Quantity(1)
    assert fill.price == Price(Decimal("18500"))


def test_replayed_fill_is_discarded_by_derived_broker_key() -> None:
    control = FakeControl()
    client = LegacyOrderApiClient(control, lambda _order: "MXFU6")
    order = _order()
    client.submit_order(order, order.client_order_id)
    client.handle_order_result("REQ-1", "SEQ1|0000|")
    fields = {
        "Oseq_No": "SEQ1",
        "Order_No": "A1234",
        "D_Time": "091031",
        "Deal_Qty": "1",
        "A_Prc": "18500",
    }

    assert client.parse_fill(fields) is not None
    assert client.parse_fill(dict(fields)) is None


def test_missing_or_malformed_broker_fields_fail_closed() -> None:
    with pytest.raises(LegacyOrderApiError):
        parse_pipe_fields("Oseq_No=1|broken")
    with pytest.raises(LegacyOrderApiError):
        parse_order_result("REQ-1", "only|two")
    with pytest.raises(LegacyOrderApiError):
        broker_fill_key({"Oseq_No": "SEQ1"})


@pytest.mark.parametrize(
    ("environment", "expected_endpoint"),
    [
        (Environment.TEST, ("apitest.yuantafutures.com.tw", 80)),
        (Environment.PRODUCTION, ("api.yuantafutures.com.tw", 443)),
    ],
)
def test_login_environment_selects_documented_simulation_or_live_endpoint(
    environment: Environment, expected_endpoint: tuple[str, int]
) -> None:
    host = FakeHost()
    broker = LegacyBroker(
        event_publisher=FakePublisher(),
        symbol_resolver=lambda _order: "MXFU6",
        host_factory=lambda: host,  # type: ignore[arg-type]
    )

    broker.start(LoginRequest(environment, "TEST-ID", SecretStr("secret")))

    assert host.control.connections == [
        ("TEST-ID", "secret", expected_endpoint[0], expected_endpoint[1])
    ]


def test_failed_connection_closes_host_and_allows_retry() -> None:
    host = FakeHost()
    close_calls: list[None] = []

    def fail_connection(_user: str, _password: str, _host: str, _port: int) -> int:
        raise OSError("COM connection failed")

    host.control.SetFutOrdConnection = fail_connection  # type: ignore[method-assign]
    host.close = lambda: close_calls.append(None)  # type: ignore[method-assign]
    broker = LegacyBroker(
        event_publisher=FakePublisher(),
        symbol_resolver=lambda _order: "MXFU6",
        host_factory=lambda: host,  # type: ignore[arg-type]
    )

    with pytest.raises(OSError, match="COM connection failed"):
        broker.start(LoginRequest(Environment.TEST, "TEST-ID", SecretStr("secret")))

    assert close_calls == [None]
    with pytest.raises(OSError, match="COM connection failed"):
        broker.start(LoginRequest(Environment.TEST, "TEST-ID", SecretStr("secret")))
    assert close_calls == [None, None]
