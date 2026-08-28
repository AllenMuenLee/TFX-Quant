from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerSessionInvalidated,
    BrokerSessionReady,
)
from tfx_quant.application.ports.broker_session import LoginRequest, SessionCapabilities
from tfx_quant.application.ports.yuanta_gateways import OrderQueryNotReadyError
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
        # Both ports come from the vendor's 使用說明.txt 連線位置 section:
        # 測試環境 Port:80, 正式環境 Port:80/443 — production may use either, and 80
        # is the configured choice.
        (Environment.TEST, ("apitest.yuantafutures.com.tw", 80)),
        (Environment.PRODUCTION, ("api.yuantafutures.com.tw", 80)),
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


def _logged_in_broker() -> tuple[LegacyBroker, FakeHost]:
    host = FakeHost()
    broker = LegacyBroker(
        event_publisher=FakePublisher(),
        symbol_resolver=lambda _order: "MXFU6",
        host_factory=lambda: host,  # type: ignore[arg-type]
    )
    broker.start(LoginRequest(Environment.TEST, "TEST-ID", SecretStr("secret")))
    host.handlers["OnLogonS"](2, "2-F00-1234567-", "", "")
    return broker, host


def test_query_open_orders_fails_closed_before_report_query_ever_completed() -> None:
    broker, _host = _logged_in_broker()

    with pytest.raises(OrderQueryNotReadyError):
        broker.query_open_orders()


def test_query_open_orders_confirms_zero_once_report_query_completes_empty() -> None:
    broker, _host = _logged_in_broker()

    broker._on_report_query(0, "")

    assert broker.query_open_orders() == ()


def test_query_open_orders_returns_currently_open_order() -> None:
    broker, _host = _logged_in_broker()
    order = _order()
    assert broker._orders is not None
    broker._orders.submit_order(order, order.client_order_id)
    broker._orders.handle_order_result("REQ-1", "SEQ1|0000|")

    broker._on_report_query(1, "Omkt=F|Statusc=0|Ts_Code=04|Oseq_No=SEQ1|R_Time=091500")

    assert broker.query_open_orders() == (order,)


def test_query_open_orders_excludes_terminal_status_orders() -> None:
    broker, _host = _logged_in_broker()
    order = _order()
    assert broker._orders is not None
    broker._orders.submit_order(order, order.client_order_id)
    broker._orders.handle_order_result("REQ-1", "SEQ1|0000|")

    broker._on_report_query(1, "Omkt=F|Statusc=0|Ts_Code=06|Oseq_No=SEQ1|R_Time=091500")

    assert broker.query_open_orders() == ()


def test_query_open_orders_uses_latest_report_not_a_stale_earlier_one() -> None:
    broker, _host = _logged_in_broker()
    order = _order()
    assert broker._orders is not None
    broker._orders.submit_order(order, order.client_order_id)
    broker._orders.handle_order_result("REQ-1", "SEQ1|0000|")

    # Acknowledged, then filled — two distinct report rows for the same order.
    broker._on_report_query(1, "Omkt=F|Statusc=0|Ts_Code=04|Oseq_No=SEQ1|R_Time=091500")
    broker._on_report_query(1, "Omkt=F|Statusc=0|Ts_Code=06|Oseq_No=SEQ1|R_Time=091600")

    assert broker.query_open_orders() == ()


def test_query_open_orders_fails_closed_on_unresolved_status() -> None:
    broker, _host = _logged_in_broker()
    order = _order()
    assert broker._orders is not None
    broker._orders.submit_order(order, order.client_order_id)
    broker._orders.handle_order_result("REQ-1", "SEQ1|0000|")

    broker._on_report_query(1, "Omkt=F|Statusc=0|Ts_Code=99|Oseq_No=SEQ1|R_Time=091500")

    with pytest.raises(OrderQueryNotReadyError):
        broker.query_open_orders()


# -- Session lifecycle: handshake, passive disconnect, reconnect -------------------------


def _fresh_broker(
    *,
    host_factory: Any | None = None,
    ui_dispatch: Any | None = None,
) -> tuple[LegacyBroker, FakeHost, FakePublisher]:
    host = FakeHost()
    publisher = FakePublisher()
    broker = LegacyBroker(
        event_publisher=publisher,
        symbol_resolver=lambda _order: "MXFU6",
        host_factory=host_factory or (lambda: host),  # type: ignore[arg-type]
        ui_dispatch=ui_dispatch,
    )
    return broker, host, publisher


def _login_request() -> LoginRequest:
    return LoginRequest(Environment.TEST, "TEST-ID", SecretStr("secret"))


def _complete_handshake(broker: LegacyBroker) -> None:
    """The three query callbacks whose arrival marks the session fully ready."""
    broker._on_report_query(0, "")
    broker._on_deal_query(0, "")
    broker._on_position_query(0, "", "RA003")


def _ready_broker() -> tuple[LegacyBroker, FakeHost, FakePublisher]:
    broker, host, publisher = _fresh_broker()
    broker.start(_login_request())
    host.handlers["OnLogonS"](2, "2-F00-1234567-", "", "")
    _complete_handshake(broker)
    return broker, host, publisher


def _of_type(publisher: FakePublisher, kind: type) -> list[Any]:
    return [event for event in publisher.events if isinstance(event, kind)]


def test_session_ready_is_published_once_per_session_not_once_per_query_callback() -> None:
    """Regression: `_mark_query_part` used to re-publish `BrokerSessionReady` on *every*
    query callback once the three parts had arrived. Each republish re-ran every
    subscriber's reconciliation sweep, whose queries produced more callbacks — a loop
    that issued ~2,900 broker queries in 16 seconds on the first successful login."""
    broker, _host, publisher = _ready_broker()

    assert len(_of_type(publisher, BrokerSessionReady)) == 1

    for _ in range(3):
        _complete_handshake(broker)

    assert len(_of_type(publisher, BrokerSessionReady)) == 1
    ready_changes = [
        event
        for event in _of_type(publisher, BrokerCapabilitiesChanged)
        if event.capabilities.is_session_ready
    ]
    assert len(ready_changes) == 1


def test_stop_clears_session_state_so_the_next_login_reruns_the_handshake() -> None:
    broker, host, publisher = _ready_broker()
    broker._on_report_query(1, "Omkt=F|Statusc=0|Ts_Code=04|Oseq_No=SEQ1|R_Time=091500")

    broker.stop()

    assert broker.accounts == ()
    assert broker.selected_account is None
    assert broker.capabilities == SessionCapabilities()
    assert broker.query_order_reports() == ()

    broker.start(_login_request())
    host.handlers["OnLogonS"](2, "2-F00-1234567-", "", "")

    # The previous session's completed parts must not carry over: a fresh session has
    # to earn its own handshake before it is announced ready.
    assert len(_of_type(publisher, BrokerSessionReady)) == 1
    _complete_handshake(broker)
    assert len(_of_type(publisher, BrokerSessionReady)) == 2


def test_terminal_logon_status_after_ready_invalidates_rather_than_fails_login() -> None:
    broker, host, publisher = _ready_broker()

    host.handlers["OnLogonS"](-1, None, "", "")

    invalidated = _of_type(publisher, BrokerSessionInvalidated)
    assert len(invalidated) == 1
    assert invalidated[0].reason == "TLinkStatus=-1"
    assert _of_type(publisher, BrokerLoginFailed) == []
    assert broker.capabilities == SessionCapabilities()


def test_logon_rejection_before_login_keeps_the_brokers_own_message() -> None:
    broker, host, publisher = _fresh_broker()
    broker.start(_login_request())

    host.handlers["OnLogonS"](-102, "00003無 api 使用權限，請洽所屬營業員！", "", "")

    failed = _of_type(publisher, BrokerLoginFailed)
    assert len(failed) == 1
    assert failed[0].reason == "TLinkStatus=-102：00003無 api 使用權限，請洽所屬營業員！"
    assert _of_type(publisher, BrokerSessionInvalidated) == []


@pytest.mark.parametrize("status", [1, 3, 100, 201, 202])
def test_transient_logon_statuses_are_progress_not_a_login_failure(status: int) -> None:
    broker, host, publisher = _fresh_broker()
    broker.start(_login_request())

    host.handlers["OnLogonS"](status, None, "", "")

    assert publisher.events == []


@pytest.mark.parametrize("status", [1, 3, 100, 201, 202])
def test_transient_logon_status_never_invalidates_a_healthy_session(status: int) -> None:
    broker, host, publisher = _ready_broker()

    host.handlers["OnLogonS"](status, None, "", "")

    assert _of_type(publisher, BrokerSessionInvalidated) == []
    assert broker.capabilities.is_session_ready


def test_reconnect_after_invalidation_replaces_the_dead_host_silently() -> None:
    hosts: list[FakeHost] = []
    closed: list[FakeHost] = []

    def factory() -> FakeHost:
        host = FakeHost()
        host.close = lambda h=host: closed.append(h)  # type: ignore[method-assign,misc]
        hosts.append(host)
        return host

    broker, _unused, publisher = _fresh_broker(host_factory=factory)
    broker.start(_login_request())
    hosts[0].handlers["OnLogonS"](2, "2-F00-1234567-", "", "")
    _complete_handshake(broker)
    hosts[0].handlers["OnLogonS"](-1, None, "", "")

    broker.start(_login_request())

    # Before this fix `start()` returned early while `_host` was set, so a reconnect
    # attempt against a dropped session silently did nothing.
    assert len(hosts) == 2
    assert closed == [hosts[0]]
    assert hosts[1].control.connections == [
        ("TEST-ID", "secret", "apitest.yuantafutures.com.tw", 80)
    ]
    # `BrokerLoggedOut` would cancel the very reconnect episode driving this call.
    assert _of_type(publisher, BrokerLoggedOut) == []


def test_start_while_a_healthy_session_is_live_is_still_a_no_op() -> None:
    hosts: list[FakeHost] = []

    def factory() -> FakeHost:
        host = FakeHost()
        hosts.append(host)
        return host

    broker, _unused, _publisher = _fresh_broker(host_factory=factory)
    broker.start(_login_request())
    hosts[0].handlers["OnLogonS"](2, "2-F00-1234567-", "", "")

    broker.start(_login_request())

    assert len(hosts) == 1


def test_start_off_the_ui_thread_is_marshalled_onto_it() -> None:
    """`ConnectivityMonitor` retries from a `threading.Timer` thread, but the OCX may
    only be created on the thread owning the wx event loop."""
    dispatched: list[Any] = []
    broker, host, _publisher = _fresh_broker(ui_dispatch=dispatched.append)
    failures: list[BaseException] = []

    def call_start() -> None:
        try:
            broker.start(_login_request())
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted on
            failures.append(exc)

    worker = threading.Thread(target=call_start)
    worker.start()
    worker.join()

    assert failures == []
    assert host.control.connections == []
    assert len(dispatched) == 1

    dispatched[0]()

    assert host.control.connections == [("TEST-ID", "secret", "apitest.yuantafutures.com.tw", 80)]


def test_start_on_the_ui_thread_is_not_marshalled() -> None:
    dispatched: list[Any] = []
    broker, host, _publisher = _fresh_broker(ui_dispatch=dispatched.append)

    broker.start(_login_request())

    assert dispatched == []
    assert len(host.control.connections) == 1
