"""Lifecycle coordinator for the persist-first Yuanta quote pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import SecretStr

from tfx_quant.application.market_data.quote_session import (
    DAY_SESSION_END,
    NIGHT_SESSION_START,
    QUOTE_HOST,
    QuoteSession,
    quote_port,
    quote_request_type,
    quote_session_at,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.quote_gateway import QuoteConnectionState, QuoteGateway
from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)


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
            try:
                self._gateway.subscribe(symbol, quote_request_type(wanted))
            except Exception as exc:  # noqa: BLE001
                log_warning(
                    _logger, "quote_subscription_retry_pending", symbol=symbol, error=str(exc)
                )
                continue
            self._subscribed += (symbol,)

    def stop_connection(self) -> None:
        gateway = self._gateway
        # Drop the local state first so a failing teardown can never leave a stale
        # gateway wired in (which would make the next `refresh()` skip the reconnect).
        self._gateway, self._session, self._subscribed = None, None, ()
        if gateway is None:
            return
        try:
            gateway.stop()
        except Exception as exc:  # noqa: BLE001
            # Closing the quote feed must never propagate. An earlier version let a
            # failing teardown bubble up through the market-data panel's refresh
            # timer and out of the wx main loop, terminating the whole application at
            # 13:45 instead of idling until the night session opened.
            self._log_teardown_failure(exc)

    def _log_teardown_failure(self, exc: Exception) -> None:
        wall = self._clock.now().value.astimezone(TAIPEI_TZ).time().replace(tzinfo=None)
        if DAY_SESSION_END <= wall < NIGHT_SESSION_START:
            # The expected daily case: the day session closed at 13:45, the vendor
            # OCX's own socket to the T server is already gone, so DelMktReg / the
            # disconnect call fails. This is not a fault — record it plainly and let
            # the scheduled 15:00 resume take over.
            log_info(
                _logger,
                "quote_connection_closed_past_day_session",
                note="過了日盤時間",
                scheduled_resume="15:00:00",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        elif quote_session_at(self._clock.now().value) is None:
            # Any other non-trading interval (e.g. the 05:00–08:45 pre-open gap).
            log_info(
                _logger,
                "quote_connection_closed_outside_session",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        else:
            log_warning(
                _logger,
                "quote_connection_stop_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def stop(self) -> None:
        self.stop_connection()
        self._user_id = self._password = None
