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

from pydantic import SecretStr

from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """The UI-built login request/credentials DTO the implementation prompt asks
    for (`login-input-implementation-prompt.md`): everything a login screen collects
    from the operator, handed to `IBrokerSession.start()` as a single call. `password`
    is a `SecretStr` so `repr()`/`str()`/logging never print the raw value — same rule
    as `infrastructure.yuanta.credentials.BrokerCredentials`, which this is
    deliberately kept separate from (that type is infrastructure-layer; this port must
    not depend on it — see the "Application does not depend on infrastructure"
    import-linter contract).

    `user_id` is the full SPARK API account string the operator types in (e.g.
    `F...`+17 digits for futures) — see 元大 SPARK API's 登入 docs page. There is no
    separate "quote session" concept in SPARK API (unlike the legacy OCX API's day/
    night quote ports) — one login covers trading, market data, and reports together.

    Deliberately does **not** carry a certificate path/password: on Windows, SPARK
    API's `Login(Account, Pass)` itself takes no certificate parameter — the docs say
    the certificate must be imported into the OS certificate store ahead of time, a
    one-time setup step independent of any individual login attempt. That one-time
    import is a `desktop/login_dialog.py`-local UI action
    (`infrastructure.yuanta.credentials.ensure_certificate_imported`), not part of the
    `Login()` RPC this DTO models — see the login screen for where it lives."""

    environment: Environment
    user_id: str
    password: SecretStr


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

    def start(self, request: LoginRequest) -> None:
        """Begin the login → query → subscribe sequence (async; publishes events).

        `request` carries the operator-entered environment, quote session, and
        credentials — built by the login UI, never guessed/defaulted by this layer
        (see `desktop/login_dialog.py`). Safe to call again after a terminal failure
        to retry manually; while a login or capped-backoff retry is already in
        progress, calling this again is a no-op.
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
        """Register for a SPARK API real-time quote symbol (e.g. "TXFH6", per 期貨報價
        代碼7xxx變更規則 — see `domain.instrument_master.futures_quote_symbol`) while
        the session is `READY`. `symbol` is already resolved (Feature 03's job — see
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
        market data, log out (`Logout()`), and tear down the underlying session/client.
        Blocks until complete. See `docs/adr/0004-broker-session-architecture.md` for
        the exact ordering."""
        ...
