from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Protocol

from pydantic import SecretStr


class QuoteLinkStatus(IntEnum):
    LINK_FAILED = -2
    LINK_BROKEN = -1
    IDLE = 0
    CONNECTED = 1
    LOGGED_ON = 2


class QuoteConnectionState(StrEnum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    LOGGED_ON = "LOGGED_ON"
    STALE = "STALE"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class QuoteUpdateMode(IntEnum):
    SNAPSHOT = 1
    UPDATE = 2
    SNAPSHOT_UPDATE = 4


class QuoteRequestType(IntEnum):
    """The vendor's feed selector, passed to and returned by every quote call.

    The installed ``YuantaQuote_v2.1.2.9.ocx`` takes this argument on ``SetMktLogon``,
    ``AddMktReg`` and ``DelMktReg``, and echoes it back as the last argument of every
    event.  It is absent from ``元大行情API.pdf`` (which documents an older, shorter
    signature) and is defined only by ``YuantaQuoteAPI Sample.py``:

        # T port 80/443 , T+1 port 82/442 ,  reqType=1 T盤 , reqType=2  T+1盤

    The OCX's own ``event.log`` labels it ``MarketType``; the type library calls it
    ``ReqType``.  It selects which of the two feed sessions (``Ses1``/``Ses2`` in the
    vendor logs) the call applies to.
    """

    T = 1
    T_PLUS_1 = 2


class QuoteGateway(Protocol):
    @property
    def state(self) -> QuoteConnectionState: ...
    def connect(
        self,
        user_id: str,
        password: SecretStr,
        host: str,
        port: int,
        request_type: QuoteRequestType,
    ) -> None: ...
    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None: ...
    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None: ...
    def stop(self) -> None: ...
