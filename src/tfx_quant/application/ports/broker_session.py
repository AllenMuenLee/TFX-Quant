"""IBrokerSession — the Yuanta login/session-lifecycle port.

Distinct from `TradeGatewayPort`/`QuoteGatewayPort` (Feature 01's narrow query/
subscribe surface, which stay as-is): this is the richer session-management surface
Feature 02 adds — login, account discovery/selection, capability tracking, and
graceful shutdown. A concrete adapter may satisfy all three Protocols at once, but
callers that only need query/subscribe access should keep depending on the narrower
ports.

`SessionCapabilities` deliberately keeps "logged in", "can receive market data", "can
trade", "can receive order/fill reports", and "can query" as five independent
booleans — the implementation prompt explicitly forbids collapsing "logged in" into
"can trade".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Protocol

from tfx_quant.domain.account import TradingAccount


@dataclass(frozen=True, slots=True)
class SessionCapabilities:
    """Independent readiness flags. See module docstring — never collapse these."""

    login: bool = False
    market_data: bool = False
    trading: bool = False
    order_reports: bool = False
    queries: bool = False

    @property
    def is_session_ready(self) -> bool:
        """True only once every capability listed above is independently true."""
        return all(getattr(self, f.name) for f in fields(self))


class LogoutReason(StrEnum):
    USER_REQUESTED = "USER_REQUESTED"
    PASSIVE_DISCONNECT = "PASSIVE_DISCONNECT"
    """The broker side dropped the connection without a local logout request."""
    SESSION_INVALIDATED = "SESSION_INVALIDATED"
    """A login/session error occurred that leaves the session unusable (bad state,
    duplicate login rejection, etc.) — not necessarily a network-level disconnect."""
    SHUTDOWN = "SHUTDOWN"
    """Local orderly application shutdown."""


class IBrokerSession(Protocol):
    """Login/session lifecycle for the Yuanta trading + quote APIs together.

    Never sends orders — order submission is Feature 06's job and has no code path
    anywhere in this codebase yet. Implementations must publish `Broker*` events
    (`application.events.events`) onto the injected `EventCoordinator` rather than
    returning results synchronously, since the underlying vendor callbacks are
    themselves asynchronous.
    """

    @property
    def capabilities(self) -> SessionCapabilities: ...

    @property
    def accounts(self) -> Sequence[TradingAccount]:
        """Futures accounts returned by the most recent successful login.

        Empty before login succeeds.
        """
        ...

    @property
    def selected_account(self) -> TradingAccount | None:
        """The account queries/subscriptions apply to. None until resolved — see
        `application.ports.broker_session` module docstring and
        `docs/adr/0004-broker-session-architecture.md` for how a unique account is
        resolved (auto-select, env var, or explicit `select_account()` call)."""
        ...

    def start(self) -> None:
        """Begin the login → query → subscribe sequence (async; publishes events).

        Safe to call again after a terminal failure to retry manually; while a login
        or capped-backoff retry is already in progress, calling this again is a no-op.
        """
        ...

    def select_account(self, account: TradingAccount) -> None:
        """Explicitly resolve the target account when more than one was returned.

        Raises if `account` is not one of `accounts`. Required before the session can
        reach `BrokerSessionReady` whenever more than one account is present and no
        other disambiguation mechanism resolved it.
        """
        ...

    def cancel_start(self) -> None:
        """Cancel an in-progress login attempt or backoff wait. Idempotent."""
        ...

    def subscribe_market_data(self, symbol: str) -> None:
        """Register for a broker-format quote symbol (e.g. "TXFE9") while the session
        is `READY`. `symbol` is already resolved (Feature 03's job — see
        `application.ports.instrument_master`); this port never translates an
        `Instrument`/`ContractMonth` itself. Raises if the session isn't ready or the
        vendor synchronously rejects the registration."""
        ...

    def unsubscribe_market_data(self, symbol: str) -> None:
        """Cancel a previous `subscribe_market_data()` registration. Safe to call for
        a symbol that was never subscribed (a no-op) — callers use this defensively
        when tearing down an old selection before switching to a new one."""
        ...

    def stop(self) -> None:
        """Orderly shutdown: verify no order is left in an unknown state, unregister
        market data, log out, and tear down the underlying adapter/host. Blocks until
        complete. See `docs/adr/0004-broker-session-architecture.md` for the exact
        ordering and the documented quote-API logout gap."""
        ...
