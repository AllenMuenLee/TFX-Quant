"""Official YuantaOrd OCX session and trade gateway."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerLoginSucceeded,
    BrokerSessionReady,
    Event,
    FillReceived,
    OrderReportReceived,
)
from tfx_quant.application.ports.broker_session import (
    IBrokerSession,
    LoginRequest,
    LogoutReason,
    SessionCapabilities,
)
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.order import ClientOrderId, Order
from tfx_quant.domain.order_state_machine import OrderReport
from tfx_quant.domain.position import Position
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.legacy_ocx_host import YuantaOcxHost
from tfx_quant.infrastructure.yuanta.legacy_order_api import (
    LegacyOrderApiClient,
    LegacyOrderApiError,
    parse_pipe_fields,
    parse_pipe_rows,
)

_ENDPOINTS = {
    Environment.TEST: ("apitest.yuantafutures.com.tw", 80),
    Environment.PRODUCTION: ("api.yuantafutures.com.tw", 443),
}

_ORDER_REPORT_FIELDS = (
    "Omkt",
    "Mktt",
    "Cmbf",
    "Statusc",
    "Ts_Code",
    "Ts_Msg",
    "Bhno",
    "Acno",
    "Suba",
    "Symb",
    "Scnam",
    "O_Kind",
    "O_Type",
    "Buys",
    "S_Buys",
    "O_Prc",
    "O_Qty",
    "Work_Qty",
    "Kill_Qty",
    "Deal_Qty",
    "Order_No",
    "T_Date",
    "O_Date",
    "O_Time",
    "O_Src",
    "O_Lin",
    "A_Prc",
    "Oseq_No",
    "Err_Code",
    "Err_Msg",
    "R_Time",
    "D_Flag",
)
_FILL_FIELDS = (
    "Omkt",
    "Buys",
    "Cmbf",
    "Bhno",
    "Acno",
    "Suba",
    "Symb",
    "Scnam",
    "O_Kind",
    "S_Buys",
    "O_Prc",
    "A_Prc",
    "O_Qty",
    "Deal_Qty",
    "T_Date",
    "D_Time",
    "Order_No",
    "O_Src",
    "O_Lin",
    "Oseq_No",
)


class EventPublisher(Protocol):
    def publish(self, event: Event) -> None: ...


class LegacyBroker(IBrokerSession):
    """One OCX instance implementing login plus the live order gateway surface."""

    def __init__(
        self,
        *,
        event_publisher: EventPublisher,
        symbol_resolver: Callable[[Order], str],
        host_factory: Callable[[], YuantaOcxHost] = YuantaOcxHost,
    ) -> None:
        self._events = event_publisher
        self._symbol_resolver = symbol_resolver
        self._host_factory = host_factory
        self._host: YuantaOcxHost | None = None
        self._orders: LegacyOrderApiClient | None = None
        self._capabilities = SessionCapabilities()
        self._accounts: tuple[TradingAccount, ...] = ()
        self._selected_account: TradingAccount | None = None
        self._reports: list[OrderReport] = []
        self._fills: list[Fill] = []
        self._positions: list[Position] = []
        self._lock = threading.RLock()
        self._query_parts: set[str] = set()

    @property
    def capabilities(self) -> SessionCapabilities:
        return self._capabilities

    @property
    def accounts(self) -> Sequence[TradingAccount]:
        return self._accounts

    @property
    def selected_account(self) -> TradingAccount | None:
        return self._selected_account

    def start(self, request: LoginRequest) -> None:
        with self._lock:
            if self._host is not None:
                return
            host = self._host_factory()
            self._host = host
            try:
                self._orders = LegacyOrderApiClient(host.control, self._symbol_resolver)
                host.bind("OnLogonS", self._on_logon)
                host.bind("OnOrdResult", self._on_order_result)
                host.bind("OnOrdRptF", self._on_order_report)
                host.bind("OnOrdMatF", self._on_fill)
                host.bind("OnReportQuery", self._on_report_query)
                host.bind("OnDealQuery", self._on_deal_query)
                host.bind("OnUserDefinsFuncResult", self._on_position_query)
                ip, port = _ENDPOINTS[request.environment]
                host.control.SetFutOrdConnection(
                    request.user_id, request.password.get_secret_value(), ip, port
                )
            except BaseException:
                host.close()
                self._host = None
                self._orders = None
                raise

    def select_account(self, account: TradingAccount) -> None:
        if account not in self._accounts:
            raise ValueError("account was not returned by OnLogonS")
        self._selected_account = account

    def cancel_start(self) -> None:
        self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._host is not None:
                self._host.close()
            self._host = None
            self._orders = None
            self._capabilities = SessionCapabilities()
            self._publish(BrokerLoggedOut(at=Timestamp.now(), reason=LogoutReason.SHUTDOWN))

    def is_logged_in(self) -> bool:
        return self._capabilities.login

    def submit_order(self, order: Order, *, client_order_id: ClientOrderId) -> None:
        if not self._capabilities.is_session_ready:
            raise LegacyOrderApiError(
                "broker reconciliation is incomplete; order submission blocked"
            )
        self._require_orders().submit_order(order, client_order_id)

    def cancel_order(self, client_order_id: ClientOrderId) -> None:
        self._require_orders().cancel_order(client_order_id)

    def query_order_reports(self) -> Sequence[OrderReport]:
        self._request_report_queries()
        return tuple(self._reports)

    def query_fills(self) -> Sequence[Fill]:
        self._request_report_queries()
        return tuple(self._fills)

    def query_open_orders(self) -> Sequence[Order]:
        return ()

    def query_positions(self) -> Sequence[Position]:
        return tuple(self._positions)

    def _on_logon(self, status: int, account_list: str, _casq: str, _cast: str) -> None:
        if status != 2:
            self._publish(
                BrokerLoginFailed(
                    at=Timestamp.now(), reason=f"TLinkStatus={status}", retriable=False
                )
            )
            return
        accounts = tuple(_parse_accounts(account_list))
        if not accounts:
            self._publish(
                BrokerLoginFailed(
                    at=Timestamp.now(), reason="OnLogonS 未回傳期貨帳號", retriable=False
                )
            )
            return
        self._accounts = accounts
        self._selected_account = accounts[0] if len(accounts) == 1 else None
        self._capabilities = SessionCapabilities(login=True, trading=False, order_reports=True)
        self._publish(
            BrokerCapabilitiesChanged(at=Timestamp.now(), capabilities=self._capabilities)
        )
        self._publish(BrokerLoginSucceeded(at=Timestamp.now(), accounts=accounts))
        if self._selected_account is not None:
            self._request_report_queries()

    def _on_order_result(self, request_id: int, result: str) -> None:
        self._require_orders().handle_order_result(str(request_id), result)

    def _on_order_report(self, *values: str) -> None:
        report = self._require_orders().parse_order_report(
            dict(zip(_ORDER_REPORT_FIELDS, values, strict=True))
        )
        if report is not None:
            self._reports.append(report)
            self._publish(OrderReportReceived(at=report.at, report=report))

    def _on_fill(self, *values: str) -> None:
        fill = self._require_orders().parse_fill(dict(zip(_FILL_FIELDS, values, strict=True)))
        if fill is not None:
            self._fills.append(fill)
            self._publish(FillReceived(at=fill.at, fill=fill))

    def _on_report_query(self, row_count: int, payload: str) -> None:
        for fields in parse_pipe_rows(payload, row_count, first_tag="Omkt"):
            self._consume_query_fields(fields, fill=False)
        self._mark_query_part("orders")

    def _on_deal_query(self, row_count: int, payload: str) -> None:
        for fields in parse_pipe_rows(payload, row_count, first_tag="Omkt"):
            self._consume_query_fields(fields, fill=True)
        self._mark_query_part("fills")

    def _consume_query_fields(self, fields: dict[str, str], *, fill: bool) -> None:
        parsed = (
            self._require_orders().parse_fill(fields)
            if fill
            else self._require_orders().parse_order_report(fields)
        )
        if isinstance(parsed, Fill):
            self._fills.append(parsed)
            self._publish(FillReceived(at=parsed.at, fill=parsed))
        elif isinstance(parsed, OrderReport):
            self._reports.append(parsed)
            self._publish(OrderReportReceived(at=parsed.at, report=parsed))

    def _on_position_query(self, row_count: int, payload: str, work_id: str) -> None:
        if work_id != "RA003":
            return
        # Position rows are deliberately not synthesized when fields cannot be mapped
        # uniquely to this system's controlled instrument master.  An empty cache is
        # only accepted when the broker explicitly declares zero rows.
        if row_count != 0:
            parse_pipe_fields(payload)  # validates the callback; parsing is fail-closed
            self._publish(
                BrokerLoginFailed(
                    at=Timestamp.now(),
                    reason="RA003 回傳非空部位；目前無法唯一映射，已禁止交易並要求人工確認",
                    retriable=False,
                )
            )
            return
        self._positions.clear()
        self._mark_query_part("positions")

    def _mark_query_part(self, part: str) -> None:
        self._query_parts.add(part)
        if self._query_parts != {"orders", "fills", "positions"}:
            return
        self._capabilities = SessionCapabilities(
            login=True, trading=True, order_reports=True, queries=True
        )
        self._publish(
            BrokerCapabilitiesChanged(at=Timestamp.now(), capabilities=self._capabilities)
        )
        assert self._selected_account is not None
        self._publish(BrokerSessionReady(at=Timestamp.now(), account=self._selected_account))

    def _request_report_queries(self) -> None:
        account = self._selected_account
        if account is None or self._host is None:
            return
        self._host.control.ReportQuery(
            "F", account.branch_id, account.account_no, account.sub_account, "0", "*", "1"
        )
        self._host.control.DealQuery(
            "F", account.branch_id, account.account_no, account.sub_account, "*"
        )
        params = (
            f"Func=RA003|bhno={account.branch_id}|acno={account.account_no}|"
            f"suba={account.sub_account}|FC=N"
        )
        self._host.control.UserDefinsFunc(params, "RA003")

    def _require_orders(self) -> LegacyOrderApiClient:
        if self._orders is None:
            raise LegacyOrderApiError("Yuanta order session is not logged in")
        return self._orders

    def _publish(self, event: Event) -> None:
        self._events.publish(event)


def _parse_accounts(raw: str) -> list[TradingAccount]:
    accounts: list[TradingAccount] = []
    for entry in raw.split(";"):
        parts = entry.split("-", 4)
        if len(parts) >= 4 and parts[0] == "2":
            accounts.append(TradingAccount(parts[1], parts[2], parts[3].strip()))
    return accounts


__all__ = ["LegacyBroker"]
