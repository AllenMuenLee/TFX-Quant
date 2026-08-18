"""YuantaQuoteOcxAdapter — the real, comtypes/wx-backed `QuoteAdapterPort`.

**Verified this session (2026-08-16)**: activation and method invocation confirmed —
but only when hosted in a real window (`OcxHost`, matching this module's design);
calling `SetMktLogon` on the control without a window raised `E_UNEXPECTED`
("Catastrophic failure"), while the identical call succeeded once hosted the normal
way. `OnMktStatusChange` was not observed firing within 20s using placeholder
credentials against the real endpoint — unconfirmed whether that's expected (the quote
API requires a separate market-data agreement beyond plain trading access — see
README — so a non-provisioned account may simply be ignored at the protocol level
rather than rejected) or an unresolved gap. See `docs/adr/0004-broker-session-
architecture.md`'s "Execution attempt findings" for the full trail; `AddMktReg`/
`DelMktReg`/`OnRegError` were not separately exercised.

**Reads the live type library, not the PDF**: this session generated Python bindings
directly from the shipped `YuantaQuote_v2.1.2.9.ocx`'s embedded type library and found
several mismatches against `元大行情API.pdf` — extra `ReqType`/`SetMap` parameters on
`SetMktLogon`/`AddMktReg`/`DelMktReg` that the PDF doesn't mention at all, and
`AddMktReg`'s `updmode` marshaled as a string (`BSTR`) rather than the int the PDF
implies. See `infrastructure/yuanta/README.md` and `vendor_codes.py` for the full
comparison. `ReqType`/`SetMap` have no documented meaning anywhere available to this
session; `vendor_codes.QUOTE_REQ_TYPE_DEFAULT`/`QUOTE_SET_MAP_DEFAULT` (both `0`) are
used here as an explicitly-flagged placeholder, not a confirmed value.

No documented quote-side logout function exists (`disconnect()` here only tears down
the local COM object/host — see `session_orchestrator.py`'s `stop()`).

**Feature 04** wires `OnGetMktAll` (the price-push event, PDF section 三.1) into
`BrokerSessionOrchestrator.handle_market_data_push` — see `market_data_parsing.py` for
the raw-field parsing and `docs/adr/0006-market-data-and-bar-aggregation.md` for the
undocumented-field gaps that parsing flags explicitly (no per-trade sequence number, no
confirmed `MatchTime` digit count). Not verified against a live push this session (no
real vendor connection available) — same open gap `OnMktStatusChange`'s firing already
carries, immediately above.
"""

from __future__ import annotations

from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.infrastructure.yuanta import com_registration
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
from tfx_quant.infrastructure.yuanta.errors import YuantaSessionError
from tfx_quant.infrastructure.yuanta.ocx_host import OcxHost
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator
from tfx_quant.infrastructure.yuanta.vendor_codes import (
    QUOTE_REQ_TYPE_DEFAULT,
    QUOTE_SET_MAP_DEFAULT,
)

_TEST_T_SESSION_HOST_PORT = ("apiquote.yuantafutures.com.tw", "80")
"""domain + T-session port, from `使用說明.txt`. The T+1 session (port 82/442) isn't
used here — Feature 02 only needs the current trading session to reach session-ready,
not the after-hours session; a later feature can add T+1 support if needed."""

_DEFAULT_UPDATE_MODE = "4"
"""SnapshotUpd (1=Snapshot, 2=Update, 4=SnapshotUpd — see README). Passed as a string:
the live `AddMktReg` dispinterface marshals `updmode` as BSTR, not int — see this
module's docstring."""


class _QuoteEventSink:
    def __init__(self, adapter: YuantaQuoteOcxAdapter, generation: int) -> None:
        self._adapter = adapter
        self._generation = generation

    def OnMktStatusChange(self, Status: int, Msg: str, ReqType: int) -> None:
        self._adapter._handle_status_changed(self._generation, Status, Msg)

    def OnRegError(self, symbol: str, updmode: int, ErrCode: int, ReqType: int) -> None:
        self._adapter._handle_registration_error(self._generation, symbol, updmode, ErrCode)

    def OnGetMktAll(
        self,
        Symbol: str,
        RefPri: str,
        OpenPri: str,
        HighPri: str,
        LowPri: str,
        UpPri: str,
        DnPri: str,
        MatchTime: str,
        MatchPri: str,
        MatchQty: str,
        TolMatchQty: str,
        BestBuyQty: str,
        BestBuyPri: str,
        BestSellQty: str,
        BestSellPri: str,
        FDBPri: str,
        FDBQty: str,
        FDSPri: str,
        FDSQty: str,
        ReqType: int,
    ) -> None:
        """All 19 fields the PDF documents, plus the trailing `ReqType` the live type
        library adds (see `infrastructure/yuanta/README.md`) — only the fields Feature
        04 actually needs are forwarded on."""
        self._adapter._handle_market_data(
            self._generation, Symbol, MatchTime, MatchPri, MatchQty, TolMatchQty
        )


class YuantaQuoteOcxAdapter:
    """Implements `session_orchestrator.QuoteAdapterPort`."""

    def __init__(self, *, environment: Environment) -> None:
        self._environment = environment
        self._host: OcxHost | None = None
        self._orchestrator: BrokerSessionOrchestrator | None = None

    def bind_orchestrator(self, orchestrator: BrokerSessionOrchestrator) -> None:
        self._orchestrator = orchestrator

    # -- QuoteAdapterPort -----------------------------------------------------------

    def connect(self, credentials: BrokerCredentials, *, generation: int) -> None:
        import os

        progid = com_registration.QUOTE_PROGID
        ocx_path = com_registration.QUOTE_OCX_PATH
        if not os.path.exists(ocx_path):
            raise YuantaSessionError(
                f"找不到元大行情 API 元件（{ocx_path!r}）；"
                "應已在啟動前檢查（preflight）階段被攔截，請依供應商安裝說明複製元件。"
            )

        self._host = OcxHost(
            progid=progid,
            ocx_path=ocx_path,
            dispatch_interface_name="_DYuantaQuote",
            events_interface_name="_DYuantaQuoteEvents",
        )
        self._host.advise(_QuoteEventSink(self, generation))

        host, port = _TEST_T_SESSION_HOST_PORT
        self._host.control.SetMktLogon(
            credentials.user_id,
            credentials.password.get_secret_value(),
            host,
            port,
            QUOTE_REQ_TYPE_DEFAULT,
            QUOTE_SET_MAP_DEFAULT,
        )

    def disconnect(self) -> None:
        if self._host is None:
            return
        # No documented logout function — see module docstring.
        self._host.close()
        self._host = None

    def subscribe(self, symbol: str) -> int:
        assert self._host is not None
        result: int = self._host.control.AddMktReg(
            symbol, _DEFAULT_UPDATE_MODE, QUOTE_REQ_TYPE_DEFAULT, QUOTE_SET_MAP_DEFAULT
        )
        return result

    def unsubscribe(self, symbol: str) -> int:
        assert self._host is not None
        result: int = self._host.control.DelMktReg(symbol, QUOTE_REQ_TYPE_DEFAULT)
        return result

    # -- Event sink -> orchestrator ------------------------------------------------

    def _handle_status_changed(self, generation: int, status: int, msg: str) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_quote_status_changed(generation, status, msg)

    def _handle_registration_error(
        self, generation: int, symbol: str, mode: int, error_code: int
    ) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_quote_registration_error(generation, symbol, mode, error_code)

    def _handle_market_data(
        self,
        generation: int,
        symbol: str,
        match_time: str,
        match_pri: str,
        match_qty: str,
        tol_match_qty: str,
    ) -> None:
        assert self._orchestrator is not None
        self._orchestrator.handle_market_data_push(
            generation, symbol, match_time, match_pri, match_qty, tol_match_qty
        )
