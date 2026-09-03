"""Thin isolation layer around YuantaQuote_v2.1.2.9.ocx callbacks.

Every signature here is the one the *installed* control declares in its type library
and uses in ``YuantaQuoteAPI Sample.py`` — not the shorter one printed in
``元大行情API.pdf``, which documents an older build.  The differences are load
bearing: calling the documented arity raises ``COMError: Parameter not optional`` and
never reaches the server.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from pydantic import SecretStr

from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteLinkStatus,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)

_SET_MAP = 0
"""The trailing ``SetMap`` argument. ``YuantaQuoteAPI Sample.py`` passes 0 on every
call; the control clamps it to a boolean and it is undocumented, so it is not exposed
as an option."""


class QuoteControl(Protocol):
    """The installed control's dispatch surface, as declared by its type library."""

    def SetMktLogon(  # noqa: N802
        self, user: str, password: str, ip: str, port: str, req_type: int, set_map: int
    ) -> None: ...
    def AddMktReg(  # noqa: N802
        self, symbol: str, updmode: str, req_type: int, set_map: int
    ) -> int: ...
    def DelMktReg(self, symbol: str, req_type: int) -> int: ...  # noqa: N802


class QuoteAdapterError(RuntimeError):
    pass


class QuoteNotLoggedOnError(QuoteAdapterError):
    pass


class QuoteRegistrationError(QuoteAdapterError):
    pass


_LOGIN_MESSAGES = {
    "0": "success",
    "1": "same user already logged in",
    "2": "invalid user",
    "3": "permission denied",
    "4": "login limit exceeded",
    "5": "authentication link failed",
    "6": "incorrect password",
    "7": "message disabled",
    "A": "password change required",
    "B": "incorrect password",
    "C": "password retry limit exceeded",
    "D": "not electronic account",
    "E": "account not found",
    "F": "password data not found",
    "G": "no usable account",
    "H": "password account association incomplete",
    "I": "certificate serial not found",
    "J": "8802 table lookup required",
    "K": "branch code error",
    "L": "market code error",
    "M": "account type error",
    "N": "broker code error",
    "O": "depository account error",
    "P": "identity number error",
    "Q": "channel code error",
    "R": "operation code error",
    "S": "failure",
    "X": "unknown error",
}

_REGISTRATION_ERRORS = {
    1: "symbol length must be 4..13",
    2: "update mode error",
    3: "connection not complete",
}

_EVENT_FIELD_NAMES = (
    "RefPri",
    "OpenPri",
    "HighPri",
    "LowPri",
    "UpPri",
    "DnPri",
    "MatchTime",
    "MatchPri",
    "MatchQty",
    "TolMatchQty",
    "BestBuyQty",
    "BestBuyPri",
    "BestSellQty",
    "BestSellPri",
    "FDBPri",
    "FDBQty",
    "FDSPri",
    "FDSQty",
)


class YuantaQuoteAdapter:
    """All methods named ``on_*`` are safe OCX event-handler entry points."""

    def __init__(
        self,
        control: QuoteControl,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._control = control
        self._on_event = on_event
        self._on_gap = on_gap
        self._clock = clock or (lambda: datetime.now(TAIPEI_TZ))
        self._state = QuoteConnectionState.IDLE
        self._session_id = uuid4().hex
        self._sequence = 0
        self._request_type: QuoteRequestType | None = None
        self._subscriptions: dict[str, QuoteUpdateMode] = {}
        self._gap_started: Timestamp | None = None
        self._event_count = 0

    @property
    def state(self) -> QuoteConnectionState:
        return self._state

    @property
    def event_count(self) -> int:
        return self._event_count

    def connect(
        self,
        user_id: str,
        password: SecretStr,
        host: str,
        port: int,
        request_type: QuoteRequestType,
    ) -> None:
        if not user_id.strip() or not host.strip() or not 1 <= port <= 65535:
            raise ValueError("valid user, host and port are required")
        self._state = QuoteConnectionState.CONNECTING
        self._session_id = uuid4().hex
        self._request_type = request_type
        log_info(
            _logger,
            "quote_login_requested",
            host=host,
            port=port,
            request_type=int(request_type),
            quote_session_id=self._session_id,
        )
        self._control.SetMktLogon(
            user_id, password.get_secret_value(), host, str(port), int(request_type), _SET_MAP
        )

    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None:
        if self._state is not QuoteConnectionState.LOGGED_ON:
            raise QuoteNotLoggedOnError("quote subscription requires successful login")
        if not 4 <= len(symbol) <= 13:
            raise ValueError("Yuanta quote symbol length must be 4..13")
        result = self._register(symbol, mode, request_type)
        if result != 0:
            raise QuoteRegistrationError(
                f"AddMktReg failed with documented RegErrCode={result}"
                f" ({_REGISTRATION_ERRORS.get(result, 'undocumented')})"
            )
        self._subscriptions[symbol] = mode

    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None:
        result = self._control.DelMktReg(symbol, int(request_type))
        log_info(
            _logger,
            "quote_unregistration_result",
            symbol=symbol,
            request_type=int(request_type),
            reg_err_code=result,
        )
        if result != 0:
            raise QuoteRegistrationError(f"DelMktReg failed with documented RegErrCode={result}")
        self._subscriptions.pop(symbol, None)

    def on_mkt_status_change(self, status: int, message: str, request_type: int) -> None:
        try:
            link = QuoteLinkStatus(status)
        except ValueError:
            log_warning(
                _logger,
                "quote_status_undocumented",
                status=status,
                request_type=request_type,
                vendor_message=message,
            )
            self._state = QuoteConnectionState.FAILED
            return
        code = message[:1]
        log_info(
            _logger,
            "quote_status_changed",
            status=status,
            link_status=link.name,
            request_type=request_type,
            message_code=code,
            message_meaning=_LOGIN_MESSAGES.get(code, "not a documented login code"),
            vendor_message=message,
        )
        if link is QuoteLinkStatus.LOGGED_ON and code == "0":
            self._state = QuoteConnectionState.LOGGED_ON
            self._close_open_gap()
            self._reregister_all(request_type)
        elif link is QuoteLinkStatus.CONNECTED:
            self._state = QuoteConnectionState.CONNECTED
        elif link in (QuoteLinkStatus.LINK_BROKEN, QuoteLinkStatus.LINK_FAILED):
            self._state = QuoteConnectionState.STALE
            self._gap_started = Timestamp(self._clock().astimezone(TAIPEI_TZ))
        elif link is QuoteLinkStatus.IDLE:
            self._state = QuoteConnectionState.IDLE
        else:
            self._state = (
                QuoteConnectionState.FAILED
                if code in _LOGIN_MESSAGES
                else QuoteConnectionState.STALE
            )

    def on_reg_error(
        self, symbol: str, update_mode: int, error_code: int, request_type: int
    ) -> None:
        del update_mode
        log_warning(
            _logger,
            "quote_registration_error",
            symbol=symbol,
            request_type=request_type,
            error_code=error_code,
        )
        self._on_gap(
            MarketDataGap(
                symbol,
                Timestamp(self._clock().astimezone(TAIPEI_TZ)),
                None,
                f"registration event ErrorCode={error_code}",
            )
        )

    def on_get_mkt_all(
        self,
        symbol: str,
        ref_pri: str,
        open_pri: str,
        high_pri: str,
        low_pri: str,
        up_pri: str,
        dn_pri: str,
        match_time: str,
        match_pri: str,
        match_qty: str,
        total_match_qty: str,
        best_buy_qty: str,
        best_buy_pri: str,
        best_sell_qty: str,
        best_sell_pri: str,
        fdb_pri: str,
        fdb_qty: str,
        fds_pri: str,
        fds_qty: str,
        request_type: int,
    ) -> None:
        del request_type
        self._sequence += 1
        self._event_count += 1
        values = (
            ref_pri,
            open_pri,
            high_pri,
            low_pri,
            up_pri,
            dn_pri,
            match_time,
            match_pri,
            match_qty,
            total_match_qty,
            best_buy_qty,
            best_buy_pri,
            best_sell_qty,
            best_sell_pri,
            fdb_pri,
            fdb_qty,
            fds_pri,
            fds_qty,
        )
        received = Timestamp(self._clock().astimezone(TAIPEI_TZ))
        self._on_event(
            RawMarketEvent(
                symbol,
                self._sequence,
                self._session_id,
                received,
                dict(zip(_EVENT_FIELD_NAMES, values, strict=True)),
            )
        )

    def stop(self) -> None:
        request_type = self._request_type
        for symbol in tuple(self._subscriptions):
            if request_type is None:
                self._subscriptions.pop(symbol, None)
            else:
                self.unsubscribe(symbol, request_type)
        self._state = QuoteConnectionState.STOPPED
        log_info(_logger, "quote_session_stopped", event_count=self._event_count)

    def _register(
        self, symbol: str, mode: QuoteUpdateMode, request_type: QuoteRequestType | int
    ) -> int:
        # The control declares ``updmode`` as a string: the sample passes ``modle[0]``,
        # the leading character of e.g. "4-SnapshotUpd".
        result = self._control.AddMktReg(symbol, str(int(mode)), int(request_type), _SET_MAP)
        log_info(
            _logger,
            "quote_registration_result",
            symbol=symbol,
            update_mode=int(mode),
            request_type=int(request_type),
            reg_err_code=result,
            reg_err_meaning=_REGISTRATION_ERRORS.get(result, "success" if result == 0 else "?"),
        )
        return result

    def _close_open_gap(self) -> None:
        if self._gap_started is None:
            return
        ended = Timestamp(self._clock().astimezone(TAIPEI_TZ))
        for symbol in self._subscriptions:
            self._on_gap(MarketDataGap(symbol, self._gap_started, ended, "disconnect"))
        self._gap_started = None

    def _reregister_all(self, request_type: int) -> None:
        for symbol, mode in tuple(self._subscriptions.items()):
            result = self._register(symbol, mode, request_type)
            if result != 0:
                self._on_gap(
                    MarketDataGap(
                        symbol,
                        Timestamp(self._clock().astimezone(TAIPEI_TZ)),
                        None,
                        f"registration failed: {result}",
                    )
                )
