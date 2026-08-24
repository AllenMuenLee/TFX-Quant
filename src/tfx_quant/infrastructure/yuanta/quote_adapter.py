"""Thin isolation layer around YuantaQuote_v2.1.2.9.ocx callbacks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from pydantic import SecretStr

from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteLinkStatus,
    QuoteUpdateMode,
)
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


class QuoteControl(Protocol):
    def SetMktLogon(self, user: str, password: str, host: str, port: str) -> None: ...
    def AddMktReg(self, symbol: str, mode: int) -> int: ...
    def DelMktReg(self, symbol: str) -> int: ...


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
        self._subscriptions: dict[str, QuoteUpdateMode] = {}
        self._gap_started: Timestamp | None = None

    @property
    def state(self) -> QuoteConnectionState:
        return self._state

    def connect(self, user_id: str, password: SecretStr, host: str, port: int) -> None:
        if not user_id.strip() or not host.strip() or not 1 <= port <= 65535:
            raise ValueError("valid user, host and port are required")
        self._state = QuoteConnectionState.CONNECTING
        self._session_id = uuid4().hex
        self._control.SetMktLogon(user_id, password.get_secret_value(), host, str(port))

    def subscribe(
        self, symbol: str, mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE
    ) -> None:
        if self._state is not QuoteConnectionState.LOGGED_ON:
            raise QuoteNotLoggedOnError("quote subscription requires successful login")
        if not 4 <= len(symbol) <= 13:
            raise ValueError("Yuanta quote symbol length must be 4..13")
        result = self._control.AddMktReg(symbol, int(mode))
        if result != 0:
            raise QuoteRegistrationError(f"AddMktReg failed with documented RegErrCode={result}")
        self._subscriptions[symbol] = mode

    def unsubscribe(self, symbol: str) -> None:
        result = self._control.DelMktReg(symbol)
        if result != 0:
            raise QuoteRegistrationError(f"DelMktReg failed with documented RegErrCode={result}")
        self._subscriptions.pop(symbol, None)

    def on_mkt_status_change(self, status: int, message: str) -> None:
        try:
            link = QuoteLinkStatus(status)
        except ValueError:
            self._state = QuoteConnectionState.FAILED
            return
        if link is QuoteLinkStatus.LOGGED_ON and message[:1] == "0":
            self._state = QuoteConnectionState.LOGGED_ON
            if self._gap_started is not None:
                ended = Timestamp(self._clock().astimezone(TAIPEI_TZ))
                for symbol in self._subscriptions:
                    self._on_gap(MarketDataGap(symbol, self._gap_started, ended, "disconnect"))
                self._gap_started = None
            for symbol, mode in tuple(self._subscriptions.items()):
                result = self._control.AddMktReg(symbol, int(mode))
                if result != 0:
                    self._on_gap(
                        MarketDataGap(
                            symbol,
                            Timestamp(self._clock().astimezone(TAIPEI_TZ)),
                            None,
                            f"registration failed: {result}",
                        )
                    )
        elif link is QuoteLinkStatus.CONNECTED:
            self._state = QuoteConnectionState.CONNECTED
        elif link in (QuoteLinkStatus.LINK_BROKEN, QuoteLinkStatus.LINK_FAILED):
            self._state = QuoteConnectionState.STALE
            self._gap_started = Timestamp(self._clock().astimezone(TAIPEI_TZ))
        elif link is QuoteLinkStatus.IDLE:
            self._state = QuoteConnectionState.IDLE
        else:
            code = message[:1]
            self._state = (
                QuoteConnectionState.FAILED
                if code in _LOGIN_MESSAGES
                else QuoteConnectionState.STALE
            )

    def on_reg_error(self, symbol: str, update_mode: int, error_code: int) -> None:
        del update_mode
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
    ) -> None:
        self._sequence += 1
        names = (
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
                dict(zip(names, values, strict=True)),
            )
        )

    def stop(self) -> None:
        for symbol in tuple(self._subscriptions):
            self.unsubscribe(symbol)
        self._state = QuoteConnectionState.STOPPED
