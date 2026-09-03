"""Lifecycle coordinator for the persist-first Yuanta quote pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import SecretStr

from tfx_quant.application.market_data.quote_session import (
    QUOTE_HOST,
    QuoteSession,
    quote_port,
    quote_request_type,
    quote_session_at,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.quote_gateway import QuoteConnectionState, QuoteGateway


class LiveQuoteService:
    """Holds one quote connection registered for *every* recorded symbol.

    Both 小台指 and 大台指 are recorded for the whole run (see
    `desktop.quote_runtime.QuoteRuntime`), so the registered set is a set, not the one
    symbol the operator happens to be charting — the vendor control accepts an
    independent ``AddMktReg`` per symbol on the same session.
    """

    def __init__(self, gateway_factory: Callable[[], QuoteGateway], clock: Clock) -> None:
        self._factory = gateway_factory
        self._clock = clock
        self._gateway: QuoteGateway | None = None
        self._user_id: str | None = None
        self._password: SecretStr | None = None
        self._session: QuoteSession | None = None
        self._symbols: tuple[str, ...] = ()
        self._subscribed: tuple[str, ...] = ()

    @property
    def state(self) -> QuoteConnectionState:
        return QuoteConnectionState.STOPPED if self._gateway is None else self._gateway.state

    def start(self, user_id: str, password: SecretStr, symbols: Iterable[str]) -> None:
        self._user_id, self._password = user_id, password
        self._symbols = tuple(symbols)
        self.refresh()

    def select_symbols(self, symbols: Iterable[str]) -> None:
        """Replace the registered set, unregistering only what is no longer wanted.

        A symbol already registered stays registered untouched, so re-selecting the
        recorded set (an instrument switch that changes only the charted market) never
        interrupts either feed.
        """
        wanted = tuple(symbols)
        gateway, session = self._gateway, self._session
        dropped = [symbol for symbol in self._subscribed if symbol not in wanted]
        if gateway is not None and session is not None:
            for symbol in dropped:
                gateway.unsubscribe(symbol, quote_request_type(session))
        self._symbols = wanted
        self._subscribed = tuple(symbol for symbol in self._subscribed if symbol not in dropped)
        self.refresh()

    def refresh(self) -> None:
        wanted = quote_session_at(self._clock.now().value)
        if wanted is None:
            self.stop_connection()
            return
        if self._user_id is None or self._password is None:
            return
        if self._gateway is None or wanted is not self._session:
            self.stop_connection()
            self._gateway = self._factory()
            self._session = wanted
            self._gateway.connect(
                self._user_id,
                self._password,
                QUOTE_HOST,
                quote_port(wanted),
                quote_request_type(wanted),
            )
        if self._gateway.state is not QuoteConnectionState.LOGGED_ON:
            return
        for symbol in self._symbols:
            if symbol in self._subscribed:
                continue
            self._gateway.subscribe(symbol, quote_request_type(wanted))
            self._subscribed += (symbol,)

    def stop_connection(self) -> None:
        if self._gateway is not None:
            self._gateway.stop()
        self._gateway, self._session, self._subscribed = None, None, ()

    def stop(self) -> None:
        self.stop_connection()
        self._user_id = self._password = None
