"""YuantaTradeOcxAdapter — the real, comtypes/wx-backed `TradeAdapterPort`.

**Verified working end-to-end this session (2026-08-16)**: activated (via
`OcxHost`/`com_registration`, no admin rights), `SetFutOrdConnection` called against
the vendor's real test endpoint with placeholder credentials, and `OnLogonS` correctly
received via a real network round-trip (`TLinkStatus=-1`, `lsLinkBroken` — the
expected outcome for placeholder credentials, not a crash or timeout). See
`docs/adr/0004-broker-session-architecture.md`'s "Execution attempt findings" for the
full trail. `ReportQuery`/`DealQuery`/`UserDefinsFunc` and their events were not
separately exercised (they need a real logged-in session), but use the same
activation/invocation/event-sink mechanics already proven to work for
`SetFutOrdConnection`/`OnLogonS`.

Method/event bindings are read at runtime from the installed `.ocx`'s own type library
via `OcxHost`/`comtypes.client.GetModule()` rather than hardcoded, which matters here
specifically because this session found the *quote* OCX's live type library disagrees
with its PDF (see `infrastructure/yuanta/README.md`) — reading the type library
directly is the only way to stay correct if the same is ever true for a future
trading-OCX build.

Every event-sink callback does the minimum possible work — parse the primitive args
into the call already waiting on `BrokerSessionOrchestrator`, nothing else — per the
implementation prompt's "callback 不阻塞；callback 內容立即複製為內部 DTO". Real
processing happens later on `EventCoordinator`'s consumer thread, not here.
"""

from __future__ import annotations

from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.infrastructure.yuanta import com_registration
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
from tfx_quant.infrastructure.yuanta.errors import YuantaSessionError
from tfx_quant.infrastructure.yuanta.ocx_host import OcxHost
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator

_TEST_HOST_PORT = ("apitest.yuantafutures.com.tw", 80)
_PRODUCTION_HOST_PORT = ("api.yuantafutures.com.tw", 80)
"""Both from `使用說明.txt`. Production also offers :443 — not used here since the
PDF/txt don't indicate a reason to prefer it over :80 for this adapter."""


class _TradeEventSink:
    """Bound to one `OcxHost.advise()` call per `connect()` — a fresh sink per
    generation, so a stale sink from a superseded connection attempt has nothing left
    advised on it (see `session_orchestrator.py`'s generation-token guard, which is
    the other half of duplicate/out-of-order-callback safety)."""

    def __init__(self, adapter: YuantaTradeOcxAdapter, generation: int) -> None:
        self._adapter = adapter
        self._generation = generation

    def OnLogonS(self, TLinkStatus: int, AccList: str, Casq: str, Cast: str) -> None:
        self._adapter._handle_login_result(self._generation, TLinkStatus, AccList, Casq, Cast)

    def OnReportQuery(self, RowCount: int, Results: str) -> None:
        self._adapter._handle_order_query_result(self._generation, RowCount, Results)

    def OnDealQuery(self, RowCount: int, Results: str) -> None:
        self._adapter._handle_deal_query_result(self._generation, RowCount, Results)

    def OnUserDefinsFuncResult(self, RowCount: int, Results: str, WorkID: str) -> None:
        self._adapter._handle_user_defined_func_result(self._generation, RowCount, Results, WorkID)


class YuantaTradeOcxAdapter:
    """Implements `session_orchestrator.TradeAdapterPort`."""

    def __init__(self, *, environment: Environment) -> None:
        self._environment = environment
        self._host: OcxHost | None = None
        self._orchestrator: BrokerSessionOrchestrator | None = None

    def bind_orchestrator(self, orchestrator: BrokerSessionOrchestrator) -> None:
        """Wires the callback direction; `composition.py` calls this once, right
        after constructing both the orchestrator and this adapter (a real dependency
        cycle — orchestrator calls out to the adapter, the adapter calls back into the
        orchestrator — resolved via this explicit two-step wiring instead of a
        constructor cycle)."""
        self._orchestrator = orchestrator

    # -- TradeAdapterPort -----------------------------------------------------------

    def connect(self, credentials: BrokerCredentials, *, generation: int) -> None:
        import os

        progid = com_registration.TRADE_PROGID_32BIT
        ocx_path = com_registration.TRADE_OCX_PATH_32BIT
        if not os.path.exists(ocx_path):
            raise YuantaSessionError(
                f"找不到元大下單 API 元件（{ocx_path!r}）；"
                "應已在啟動前檢查（preflight）階段被攔截，請依供應商安裝說明複製元件。"
            )

        self._host = OcxHost(
            progid=progid,
            ocx_path=ocx_path,
            dispatch_interface_name="_DYuantaOrd",
            events_interface_name="_DYuantaOrdEvents",
        )
        self._host.advise(_TradeEventSink(self, generation))

        host, port = (
            _TEST_HOST_PORT if self._environment is Environment.TEST else _PRODUCTION_HOST_PORT
        )
        self._host.control.SetFutOrdConnection(
            credentials.user_id, credentials.password.get_secret_value(), host, port
        )

    def disconnect(self) -> None:
        if self._host is None:
            return
        try:
            self._host.control.DoLogout()
        finally:
            self._host.close()
            self._host = None

    def query_open_orders(self, account: TradingAccount) -> None:
        assert self._host is not None
        # Stus="0" (全部, all statuses) — the safety checklist needs to see every
        # order, not only cancelable ones. Cflg="1" (當日委託, today's orders).
        self._host.control.ReportQuery(
            "F", account.branch_id, account.account_no, account.sub_account, "0", "*", "1"
        )

    def query_fills(self, account: TradingAccount) -> None:
        assert self._host is not None
        self._host.control.DealQuery(
            "F", account.branch_id, account.account_no, account.sub_account, "*"
        )

    def query_positions(self, account: TradingAccount) -> None:
        assert self._host is not None
        # Func=RA003 (部位狀況查詢) — see session_orchestrator.py's
        # handle_user_defined_func_result docstring for why the result isn't parsed
        # into a typed Position here.
        params = (
            f"Func=RA003|bhno={account.branch_id}|acno={account.account_no}"
            f"|suba={account.sub_account}|FC=N"
        )
        self._host.control.UserDefinsFunc(params, "RA003")

    # -- Event sink -> orchestrator ------------------------------------------------

    def _handle_login_result(
        self, generation: int, tlink_status: int, acc_list: str, casq: str, cast: str
    ) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_trade_login_result(generation, tlink_status, acc_list, casq, cast)

    def _handle_order_query_result(self, generation: int, row_count: int, results: str) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_order_query_result(generation, row_count, results)

    def _handle_deal_query_result(self, generation: int, row_count: int, results: str) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_deal_query_result(generation, row_count, results)

    def _handle_user_defined_func_result(
        self, generation: int, row_count: int, results: str, work_id: str
    ) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_user_defined_func_result(generation, row_count, results, work_id)
