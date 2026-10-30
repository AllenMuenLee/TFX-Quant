"""Lifecycle coordinator for the persist-first Yuanta quote pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta

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
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)

_RECONNECT_AFTER = timedelta(seconds=15)
_SILENT_FEED_RECONNECT_AFTER = timedelta(seconds=30)

MarketOpenCheck = Callable[[Timestamp], bool]
ForcedRecoveryHandler = Callable[[Timestamp, Timestamp, str], None]


class LiveQuoteService:
    """Holds one quote connection registered for *every* recorded symbol.

    Both 小台指 and 大台指 are recorded for the whole run (see
    `desktop.quote_runtime.QuoteRuntime`), so the registered set is a set, not the one
    symbol the operator happens to be charting — the vendor control accepts an
    independent ``AddMktReg`` per symbol on the same session.
    """

    def __init__(
        self,
        gateway_factory: Callable[[], QuoteGateway],
        clock: Clock,
        *,
        market_is_open: MarketOpenCheck | None = None,
        on_forced_recovery: ForcedRecoveryHandler | None = None,
    ) -> None:
        self._factory = gateway_factory
        self._clock = clock
        self._gateway: QuoteGateway | None = None
        self._user_id: str | None = None
        self._password: SecretStr | None = None
        self._session: QuoteSession | None = None
        self._symbols: tuple[str, ...] = ()
        self._subscribed: tuple[str, ...] = ()
        self._connection_attempted_at: Timestamp | None = None
        self._last_event_at: dict[str, Timestamp] = {}
        self._market_is_open = market_is_open or (lambda _at: True)
        self._on_forced_recovery = on_forced_recovery
        self._forced_recovery_count = 0
        self._last_recovery_reason: str | None = None

    @property
    def state(self) -> QuoteConnectionState:
        return QuoteConnectionState.STOPPED if self._gateway is None else self._gateway.state

    @property
    def forced_recovery_count(self) -> int:
        return self._forced_recovery_count

    @property
    def last_recovery_reason(self) -> str | None:
        return self._last_recovery_reason

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

    def note_event(self, symbol: str, received_at: Timestamp) -> None:
        """Record proof that one subscribed symbol still delivers valid trades."""
        self._last_event_at[symbol] = received_at

    def invalidate_subscription(self, symbol: str) -> None:
        """Make an asynchronous registration error retry on the next refresh."""
        self._subscribed = tuple(item for item in self._subscribed if item != symbol)

    def refresh(self) -> None:
        now = self._clock.now()
        wanted = quote_session_at(now.value)
        if wanted is None:
            self.stop_connection()
            return
        if self._user_id is None or self._password is None:
            return
        if (
            self._gateway is not None
            and wanted is self._session
            and self._gateway.state
            in (
                QuoteConnectionState.CONNECTING,
                QuoteConnectionState.CONNECTED,
                QuoteConnectionState.STALE,
                QuoteConnectionState.FAILED,
                QuoteConnectionState.IDLE,
                QuoteConnectionState.STOPPED,
            )
            and self._connection_attempted_at is not None
            and now.value - self._connection_attempted_at.value >= _RECONNECT_AFTER
        ):
            failed_at = self._connection_attempted_at
            log_warning(
                _logger,
                "quote_connection_restarting",
                quote_state=self._gateway.state.value,
                session=wanted.value,
            )
            self._report_forced_recovery(failed_at, now, f"quote state {self._gateway.state.value}")
            self.stop_connection()
        silent_symbols: list[str] = []
        oldest_activity = self._connection_attempted_at
        if self._connection_attempted_at is not None:
            for symbol in self._symbols:
                activity = self._last_event_at.get(symbol)
                if activity is None or activity.value < self._connection_attempted_at.value:
                    activity = self._connection_attempted_at
                if now.value - activity.value >= _SILENT_FEED_RECONNECT_AFTER:
                    silent_symbols.append(symbol)
                    if oldest_activity is None or activity.value < oldest_activity.value:
                        oldest_activity = activity
        if (
            self._gateway is not None
            and wanted is self._session
            and self._gateway.state is QuoteConnectionState.LOGGED_ON
            and oldest_activity is not None
            and self._market_is_open(now)
            and silent_symbols
        ):
            log_warning(
                _logger,
                "quote_silent_feed_restarting",
                session=wanted.value,
                silent_symbols=silent_symbols,
                last_events={
                    symbol: (
                        None
                        if self._last_event_at.get(symbol) is None
                        else self._last_event_at[symbol].value.isoformat()
                    )
                    for symbol in silent_symbols
                },
            )
            self._report_forced_recovery(
                oldest_activity,
                now,
                f"logged on but quote feed is silent: {', '.join(silent_symbols)}",
            )
            self.stop_connection()
        if self._gateway is None or wanted is not self._session:
            self.stop_connection()
            self._gateway = self._factory()
            self._session = wanted
            self._connection_attempted_at = now
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

    def _report_forced_recovery(
        self, started_at: Timestamp, ended_at: Timestamp, reason: str
    ) -> None:
        self._forced_recovery_count += 1
        self._last_recovery_reason = reason
        if self._on_forced_recovery is not None and self._market_is_open(ended_at):
            self._on_forced_recovery(started_at, ended_at, reason)

    def stop_connection(self) -> None:
        gateway = self._gateway
        # Drop the local state first so a failing teardown can never leave a stale
        # gateway wired in (which would make the next `refresh()` skip the reconnect).
        self._gateway, self._session, self._subscribed = None, None, ()
        self._connection_attempted_at = None
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
