"""Lifecycle coordinator for the persist-first Yuanta quote pipeline."""

from __future__ import annotations

from collections.abc import Callable

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
    def __init__(self, gateway_factory: Callable[[], QuoteGateway], clock: Clock) -> None:
        self._factory = gateway_factory
        self._clock = clock
        self._gateway: QuoteGateway | None = None
        self._user_id: str | None = None
        self._password: SecretStr | None = None
        self._session: QuoteSession | None = None
        self._symbol: str | None = None
        self._subscribed: str | None = None

    @property
    def state(self) -> QuoteConnectionState:
        return QuoteConnectionState.STOPPED if self._gateway is None else self._gateway.state

    def start(self, user_id: str, password: SecretStr, symbol: str) -> None:
        self._user_id, self._password, self._symbol = user_id, password, symbol
        self.refresh()

    def select_symbol(self, symbol: str) -> None:
        gateway, session = self._gateway, self._session
        if gateway is not None and self._subscribed is not None and session is not None:
            gateway.unsubscribe(self._subscribed, quote_request_type(session))
        self._symbol, self._subscribed = symbol, None
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
        if (
            self._gateway.state is QuoteConnectionState.LOGGED_ON
            and self._symbol is not None
            and self._subscribed != self._symbol
        ):
            self._gateway.subscribe(self._symbol, quote_request_type(wanted))
            self._subscribed = self._symbol

    def stop_connection(self) -> None:
        if self._gateway is not None:
            self._gateway.stop()
        self._gateway, self._session, self._subscribed = None, None, None

    def stop(self) -> None:
        self.stop_connection()
        self._user_id = self._password = None
