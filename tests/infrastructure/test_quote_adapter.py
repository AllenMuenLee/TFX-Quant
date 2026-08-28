"""The adapter is pinned to the signatures the installed control actually uses.

Every argument list here is taken from ``YuantaQuoteAPI Sample.py`` and from a live
session against the production feed: ``SetMktLogon`` takes six arguments, ``AddMktReg``
four (with a *string* update mode), ``DelMktReg`` two, and every event ends with the
``ReqType`` the call was made on.  ``元大行情API.pdf`` documents the older, shorter
signatures; calling those raises ``COMError: Parameter not optional``.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import SecretStr

from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.infrastructure.yuanta.quote_adapter import (
    QuoteNotLoggedOnError,
    QuoteRegistrationError,
    YuantaQuoteAdapter,
)
from tfx_quant.infrastructure.yuanta.quote_com_host import _EventSink

_LOGON_OK = "0行情連線登入成功!"
_CONNECTED = "行情連線成功,等待登入中!"
# One real MXFI6 callback, captured 2026-08-28 09:48 on reqType=1.
_TICK = (
    "MXFI6",
    "46064",
    "46380",
    "46574",
    "46270",
    "50670",
    "41458",
    "094838038000",
    "46291",
    "1",
    "40018",
    "1,1,4,5,1",
    "46290,46289,46288,46287,46286",
    "5,4,3,5,5",
    "46292,46293,46294,46295,46296",
    "0",
    "0",
    "0",
    "0",
)


class FakeControl:
    def __init__(self, reg_result: int = 0) -> None:
        self.logons: list[tuple[str, str, str, str, int, int]] = []
        self.registrations: list[tuple[str, str, int, int]] = []
        self.deletions: list[tuple[str, int]] = []
        self.reg_result = reg_result

    def SetMktLogon(  # noqa: N802
        self, user: str, password: str, ip: str, port: str, req_type: int, set_map: int
    ) -> None:
        self.logons.append((user, password, ip, port, req_type, set_map))

    def AddMktReg(  # noqa: N802
        self, symbol: str, updmode: str, req_type: int, set_map: int
    ) -> int:
        self.registrations.append((symbol, updmode, req_type, set_map))
        return self.reg_result

    def DelMktReg(self, symbol: str, req_type: int) -> int:  # noqa: N802
        self.deletions.append((symbol, req_type))
        return 0


def _adapter(
    control: FakeControl,
    events: list[RawMarketEvent] | None = None,
    gaps: list[MarketDataGap] | None = None,
) -> YuantaQuoteAdapter:
    return YuantaQuoteAdapter(
        control,
        (events if events is not None else []).append,
        (gaps if gaps is not None else []).append,
        lambda: datetime(2026, 8, 28, 9, 48, 38, tzinfo=TAIPEI_TZ),
    )


def test_connect_passes_request_type_and_set_map() -> None:
    control = FakeControl()
    adapter = _adapter(control)

    adapter.connect("E123456789", SecretStr("secret"), "apiquote.example", 80, QuoteRequestType.T)

    assert control.logons == [("E123456789", "secret", "apiquote.example", "80", 1, 0)]
    assert adapter.state is QuoteConnectionState.CONNECTING


def test_subscription_requires_login_and_sends_string_update_mode() -> None:
    control = FakeControl()
    adapter = _adapter(control)

    with pytest.raises(QuoteNotLoggedOnError):
        adapter.subscribe("MXFI6", QuoteRequestType.T)

    adapter.on_mkt_status_change(2, _LOGON_OK, 1)
    adapter.subscribe("MXFI6", QuoteRequestType.T)

    assert adapter.state is QuoteConnectionState.LOGGED_ON
    assert control.registrations == [("MXFI6", "4", 1, 0)]


def test_documented_registration_error_is_raised_with_its_meaning() -> None:
    control = FakeControl(reg_result=3)
    adapter = _adapter(control)
    adapter.on_mkt_status_change(2, _LOGON_OK, 1)

    with pytest.raises(QuoteRegistrationError, match="RegErrCode=3"):
        adapter.subscribe("MXFI6", QuoteRequestType.T)


def test_connected_status_is_not_treated_as_logged_on() -> None:
    adapter = _adapter(FakeControl())
    adapter.on_mkt_status_change(1, _CONNECTED, 1)
    assert adapter.state is QuoteConnectionState.CONNECTED


def test_login_rejection_code_fails_the_session() -> None:
    adapter = _adapter(FakeControl())
    adapter.on_mkt_status_change(2, "6密碼錯誤", 1)
    assert adapter.state is QuoteConnectionState.FAILED


def test_broken_link_marks_gap_and_relogin_reregisters_and_closes_it() -> None:
    control = FakeControl()
    gaps: list[MarketDataGap] = []
    adapter = _adapter(control, gaps=gaps)
    adapter.on_mkt_status_change(2, _LOGON_OK, 1)
    adapter.subscribe("MXFI6", QuoteRequestType.T)

    adapter.on_mkt_status_change(-1, "斷線", 1)
    assert adapter.state is QuoteConnectionState.STALE

    adapter.on_mkt_status_change(2, _LOGON_OK, 1)
    assert [gap.reason for gap in gaps] == ["disconnect"]
    assert gaps[0].end is not None
    assert control.registrations == [("MXFI6", "4", 1, 0), ("MXFI6", "4", 1, 0)]


def test_reregistration_failure_opens_an_unbounded_gap() -> None:
    control = FakeControl()
    gaps: list[MarketDataGap] = []
    adapter = _adapter(control, gaps=gaps)
    adapter.on_mkt_status_change(2, _LOGON_OK, 1)
    adapter.subscribe("MXFI6", QuoteRequestType.T)
    control.reg_result = 3

    adapter.on_mkt_status_change(2, _LOGON_OK, 1)

    assert gaps[-1].reason == "registration failed: 3"
    assert gaps[-1].end is None


def test_twenty_argument_callback_is_recorded_with_documented_field_names() -> None:
    events: list[RawMarketEvent] = []
    adapter = _adapter(FakeControl(), events=events)

    adapter.on_get_mkt_all(*_TICK, 1)

    event = events[0]
    assert event.symbol == "MXFI6"
    assert event.sequence == 1
    assert event.fields["MatchTime"] == "094838038000"
    assert event.fields["MatchPri"] == "46291"
    assert event.fields["TolMatchQty"] == "40018"
    assert event.fields["BestBuyPri"] == "46290,46289,46288,46287,46286"
    assert adapter.event_count == 1


def test_registration_error_event_records_a_gap() -> None:
    gaps: list[MarketDataGap] = []
    adapter = _adapter(FakeControl(), gaps=gaps)

    adapter.on_reg_error("MXFI6", int(QuoteUpdateMode.SNAPSHOT_UPDATE), 2, 1)

    assert gaps[0].symbol == "MXFI6"
    assert "ErrorCode=2" in gaps[0].reason


def test_stop_unregisters_on_the_session_it_logged_into() -> None:
    control = FakeControl()
    adapter = _adapter(control)
    adapter.connect("E123456789", SecretStr("s"), "apiquote.example", 82, QuoteRequestType.T_PLUS_1)
    adapter.on_mkt_status_change(2, _LOGON_OK, 2)
    adapter.subscribe("MXFI6", QuoteRequestType.T_PLUS_1)

    adapter.stop()

    assert control.deletions == [("MXFI6", 2)]
    assert adapter.state is QuoteConnectionState.STOPPED


def test_explicit_interface_event_sink_receives_callbacks_without_com_this() -> None:
    events: list[RawMarketEvent] = []
    gaps: list[MarketDataGap] = []
    adapter = _adapter(FakeControl(), events=events, gaps=gaps)
    sink = _EventSink(adapter)

    # GetEvents(..., interface=_DYuantaQuoteEvents) installs comtypes' without_this
    # wrapper, so these are exactly the arguments delivered to the Python sink.
    sink.OnMktStatusChange(2, _LOGON_OK, 1)
    sink.OnRegError("MXFI6", int(QuoteUpdateMode.SNAPSHOT_UPDATE), 2, 1)
    sink.OnGetMktAll(*_TICK, 1)

    assert adapter.state is QuoteConnectionState.LOGGED_ON
    assert gaps[0].symbol == "MXFI6"
    assert events[0].symbol == "MXFI6"
    assert events[0].fields["MatchPri"] == "46291"
