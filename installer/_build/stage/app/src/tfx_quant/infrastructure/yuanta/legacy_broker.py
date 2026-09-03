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
    BrokerSessionInvalidated,
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
from tfx_quant.application.ports.yuanta_gateways import OrderQueryNotReadyError
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.order import ClientOrderId, Order
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.legacy_ocx_host import YuantaOcxHost
from tfx_quant.infrastructure.yuanta.legacy_order_api import (
    LegacyOrderApiClient,
    LegacyOrderApiError,
    parse_pipe_fields,
    parse_pipe_rows,
)

# The only real trade endpoint. There is no "UAT/test trade server" flow any more — the
# 測試環境 uses the local broker simulator (`MockBrokerSession`/`MockTradeGateway`), which
# `desktop.composition.build_services` wires whenever `settings.environment` is TEST, so
# this adapter is only ever constructed for PRODUCTION and `LoginRequest.environment` no
# longer routes the connection.
_PRODUCTION_ENDPOINT = ("api.yuantafutures.com.tw", 443)

_LOGON_OK = 2
"""`OnLogonS`'s `TLinkStatus` = `lsLogonOK`, the only value that means "session up"."""

_TRANSIENT_LOGON_STATUSES = frozenset({1, 3, 100, 201, 202})
"""`lsConnected`/`lsConnecting`/`lsQuerySending`/`lsLogoning1`/`lsLogoning2` — progress
reports, not outcomes.  The vendor may deliver them before the real result, so they must
never be mistaken for a failure (which, post-ready, would invalidate a healthy session).
Everything else that is not `_LOGON_OK` is terminal: -1 `lsLinkBroken`, 0 `lsIdle`,
4 `lsCAError`, 5 `lsPassError`, plus undocumented codes such as -102 (entitlement
rejection), which carry their server-side message in the `AccList` argument."""


def _logon_failure_reason(status: int, message: object) -> str:
    """The vendor packs its human-readable rejection text into `OnLogonS`'s `AccList`
    argument on failure (-102 sends "無 api 使用權限，請洽所屬營業員！"; -1 sends NULL),
    so the reason string keeps it whenever one is present."""
    text = str(message).strip() if message is not None else ""
    return f"TLinkStatus={status}：{text}" if text else f"TLinkStatus={status}"


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


_OPEN_ORDER_STATUSES = frozenset(
    {OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}
)


class LegacyBroker(IBrokerSession):
    """One OCX instance implementing login plus the live order gateway surface."""

    def __init__(
        self,
        *,
        event_publisher: EventPublisher,
        symbol_resolver: Callable[[Order], str],
        host_factory: Callable[[], YuantaOcxHost] = YuantaOcxHost,
        ui_dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        """`ui_dispatch` marshals a callable onto the wx UI thread (`wx.CallAfter` in
        `desktop.composition`).  It exists because `ConnectivityMonitor`'s reconnect
        attempts call `start()` from a `threading.Timer` thread, while `YuantaOcxHost`
        may only be created, bound, and closed on the thread that owns the wx event
        loop.  Left `None` (tests, and any caller already on the UI thread) `start()`
        runs inline exactly as before."""
        self._events = event_publisher
        self._symbol_resolver = symbol_resolver
        self._host_factory = host_factory
        self._ui_dispatch = ui_dispatch
        self._ui_thread_id = threading.get_ident()
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
        self._session_invalid = False

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
        """Asynchronous: the outcome always arrives as `BrokerLoginSucceeded` /
        `BrokerLoginFailed`, never as a return value or a raise from here once the
        call has been marshalled onto the UI thread."""
        if self._ui_dispatch is not None and threading.get_ident() != self._ui_thread_id:
            self._ui_dispatch(lambda: self._start_on_ui_thread(request))
            return
        self._start_on_ui_thread(request)

    def _start_on_ui_thread(self, request: LoginRequest) -> None:
        with self._lock:
            if self._host is not None and not self._session_invalid:
                return
            if self._host is not None:
                # A reconnect over the corpse of a session the broker already dropped.
                # Torn down *here* rather than in `_on_logon` because that runs inside
                # the OCX's own event dispatch, where destroying the control is unsafe.
                # Silent on purpose: `BrokerLoggedOut` would cancel the very reconnect
                # episode that called us (`ConnectivityMonitor._on_logged_out`).
                self._teardown_locked()
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
                # `request.environment` no longer routes the connection: there is exactly
                # one real trade endpoint. `build_services` only ever constructs this
                # adapter for `Environment.PRODUCTION`; a TEST `LoginRequest` is served by
                # the local broker simulator and never reaches here.
                ip, port = _PRODUCTION_ENDPOINT
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
            self._teardown_locked()
            self._publish(BrokerLoggedOut(at=Timestamp.now(), reason=LogoutReason.SHUTDOWN))

    def _teardown_locked(self) -> None:
        """Drops every trace of the current session.  The caches and `_query_parts` are
        per-session broker truth: carrying them into the next login would let the first
        callback of a fresh session complete a handshake it never performed, and would
        serve the previous session's reports and fills as if they were current."""
        if self._host is not None:
            self._host.close()
        self._host = None
        self._orders = None
        self._capabilities = SessionCapabilities()
        self._accounts = ()
        self._selected_account = None
        self._reports.clear()
        self._fills.clear()
        self._positions.clear()
        self._query_parts.clear()
        self._session_invalid = False

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
        """Derives currently-open orders from the same report cache
        `query_order_reports()` reads — `ReportQuery` is the only documented call this
        codebase issues for order state, so "open" is a filter over its results, not a
        separate vendor query. Fails closed (raises `OrderQueryNotReadyError`) rather
        than answering "zero" whenever the answer isn't actually knowable yet: before
        the report query has completed even once this session, or when any order's
        latest known report resolved to `OrderStatus.UNKNOWN`. See
        `OrderQueryNotReadyError`'s docstring for the caller contract."""
        self._request_report_queries()
        if "orders" not in self._query_parts:
            raise OrderQueryNotReadyError(
                "order report query has not completed for this broker session yet"
            )
        latest_by_order: dict[ClientOrderId, OrderReport] = {}
        for report in self._reports:
            current = latest_by_order.get(report.client_order_id)
            if current is None or report.broker_seq_no > current.broker_seq_no:
                latest_by_order[report.client_order_id] = report
        open_orders: list[Order] = []
        for report in latest_by_order.values():
            if report.status is OrderStatus.UNKNOWN:
                raise OrderQueryNotReadyError(
                    "an order report's status could not be resolved; "
                    "cannot confirm whether it is still open"
                )
            if report.status not in _OPEN_ORDER_STATUSES:
                continue
            order = self._require_orders().order_for(report.client_order_id)
            if order is None:
                raise OrderQueryNotReadyError(
                    "an open order report has no locally-tracked Order to describe it"
                )
            open_orders.append(order)
        return tuple(open_orders)

    def query_positions(self) -> Sequence[Position]:
        return tuple(self._positions)

    def _on_logon(self, status: int, account_list: str, _casq: str, _cast: str) -> None:
        status = int(status)
        if status in _TRANSIENT_LOGON_STATUSES:
            return
        if status != _LOGON_OK:
            reason = _logon_failure_reason(status, account_list)
            if self._capabilities.login:
                # The OCX re-fires `OnLogonS` with a terminal status when an already
                # established link drops — the only passive-disconnect signal this
                # control offers.  That is a lost session, not a rejected login.
                self._invalidate(reason)
            else:
                self._publish(BrokerLoginFailed(at=Timestamp.now(), reason=reason, retriable=False))
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

    def _invalidate(self, reason: str) -> None:
        """Marks the live session dead without touching COM.  The host is deliberately
        left standing for `_start_on_ui_thread` to close: this runs inside the OCX's own
        event callback, where closing the control that is dispatching to us risks
        tearing down the object mid-dispatch."""
        self._session_invalid = True
        self._capabilities = SessionCapabilities()
        # Cleared so the next session has to earn its own handshake from scratch;
        # otherwise its first callback would find the set already complete.
        self._query_parts.clear()
        self._publish(
            BrokerCapabilitiesChanged(at=Timestamp.now(), capabilities=self._capabilities)
        )
        self._publish(BrokerSessionInvalidated(at=Timestamp.now(), reason=reason))

    def _mark_query_part(self, part: str) -> None:
        self._query_parts.add(part)
        if self._query_parts != {"orders", "fills", "positions"}:
            return
        if self._capabilities.is_session_ready:
            # Every later query callback re-enters here with the set already complete.
            # Re-publishing would re-run every `BrokerSessionReady` subscriber's
            # reconciliation sweep, whose queries land back here — an unbounded loop
            # that saturated the broker link at ~60 query triples/second on the first
            # session that ever got far enough to reach this line.
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
