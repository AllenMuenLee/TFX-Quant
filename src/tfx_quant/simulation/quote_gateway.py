from __future__ import annotations

from collections.abc import Callable

from pydantic import SecretStr

from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent


class SimulatedQuoteGateway:
    """In-memory fake for the documented quote connection/registration surface."""

    def __init__(
        self,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
    ) -> None:
        self._on_event, self._on_gap = on_event, on_gap
        self._state = QuoteConnectionState.IDLE
        self._symbols: set[str] = set()

    @property
    def state(self) -> QuoteConnectionState:
        return self._state

    def connect(
        self,
        user_id: str,
        password: SecretStr,
        host: str,
        port: int,
        request_type: QuoteRequestType,
    ) -> None:
        del user_id, password, host, port, request_type
        self._state = QuoteConnectionState.LOGGED_ON

    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None:
        del request_type
        if self._state is not QuoteConnectionState.LOGGED_ON:
            raise ConnectionError("simulated quote session is not logged on")
        documented_modes = (
            QuoteUpdateMode.SNAPSHOT,
            QuoteUpdateMode.UPDATE,
            QuoteUpdateMode.SNAPSHOT_UPDATE,
        )
        if mode not in documented_modes:
            raise ValueError("unsupported documented update mode")
        self._symbols.add(symbol)

    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None:
        del request_type
        self._symbols.discard(symbol)

    def stop(self) -> None:
        self._symbols.clear()
        self._state = QuoteConnectionState.STOPPED

    def emit(self, event: RawMarketEvent) -> None:
        if self._state is not QuoteConnectionState.LOGGED_ON:
            raise ConnectionError("simulated quote session is not logged on")
        if event.symbol not in self._symbols:
            raise ValueError("fixture event symbol is not registered")
        self._on_event(event)

    def emit_gap(self, gap: MarketDataGap) -> None:
        self._on_gap(gap)

    def break_link(self) -> None:
        self._state = QuoteConnectionState.STALE
