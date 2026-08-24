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


class QuoteGateway(Protocol):
    @property
    def state(self) -> QuoteConnectionState: ...
    def connect(self, user_id: str, password: SecretStr, host: str, port: int) -> None: ...
    def subscribe(
        self, symbol: str, mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE
    ) -> None: ...
    def unsubscribe(self, symbol: str) -> None: ...
    def stop(self) -> None: ...
