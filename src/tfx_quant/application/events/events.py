"""Internal event shapes.

Broker callbacks (COM events, arriving on whatever thread the OCX message pump runs
on) are translated into one of these immutable event objects *before* being handed to
the `EventCoordinator` — nothing downstream of the coordinator ever touches a raw
vendor callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from tfx_quant.application.ports.broker_session import LogoutReason, SessionCapabilities
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import Order
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all internal events."""

    at: Timestamp


@dataclass(frozen=True, slots=True)
class ConnectionStatusChanged(Event):
    gateway_name: str
    is_connected: bool


@dataclass(frozen=True, slots=True)
class OrderReportReceived(Event):
    order: Order


@dataclass(frozen=True, slots=True)
class FillReceived(Event):
    fill: Fill


@dataclass(frozen=True, slots=True)
class BrokerLoginSucceeded(Event):
    """SPARK API's `Login` result (`OnResponse`, `strIndex == 'Login'`) reported
    success. `accounts` is the parsed, futures-only account list — session-ready still
    requires the safety queries + market data subscription too."""

    accounts: tuple[TradingAccount, ...]


@dataclass(frozen=True, slots=True)
class BrokerLoginFailed(Event):
    """A login attempt failed with a broker-reported reason.

    `retriable` reflects whether `BrokerSessionOrchestrator`'s backoff policy will
    attempt again automatically (e.g. a transient network failure) versus a terminal
    misconfiguration (e.g. wrong password) that requires user intervention.
    """

    reason: str
    retriable: bool


@dataclass(frozen=True, slots=True)
class BrokerLoginTimedOut(Event):
    """No `Login` result arrived via `OnResponse` within the configured timeout."""


@dataclass(frozen=True, slots=True)
class BrokerDuplicateLoginRejected(Event):
    """The broker reported the same user ID is already logged in elsewhere.

    Only the quote API documents an explicit code for this (`OnMktStatusChange`
    `Msg[0] == '1'`, UserIDTheSame) — see `infrastructure/yuanta/README.md`. The
    trading API has no distinct code for this case and surfaces it as a generic
    `BrokerLoginFailed` instead.
    """

    source: str
    """Which OCX reported it — "trade" or "quote"."""


@dataclass(frozen=True, slots=True)
class BrokerLoggedOut(Event):
    reason: LogoutReason


@dataclass(frozen=True, slots=True)
class BrokerSessionInvalidated(Event):
    """A post-ready session became unusable (passive disconnect, broker-side error).

    This is the safe-pause hook: Feature 09 subscribes to this to drive
    `StrategyStateMachine` toward `PausedSafe`/`Faulted`. Feature 02 only guarantees
    this fires reliably — it does not implement the pause itself.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class BrokerCapabilitiesChanged(Event):
    capabilities: SessionCapabilities


@dataclass(frozen=True, slots=True)
class BrokerSessionReady(Event):
    """Login, order query, fill query, position query, and market data subscription
    all succeeded — every `SessionCapabilities` flag is now true."""

    account: TradingAccount


@dataclass(frozen=True, slots=True)
class InstrumentSwitchCompleted(Event):
    """`InstrumentSelectionService.switch_to()` succeeded — old quote subscription
    cancelled, account status requeried, new contract subscribed, bar/signal state
    cleared. The strategy state machine is left in whatever paused/stopped state it
    was already in; per Feature 03's acceptance criteria the user must still press
    start again (Running is only ever reachable via a fresh Starting)."""

    instrument: Instrument
    contract: ContractMonth
    vendor_symbol: str


@dataclass(frozen=True, slots=True)
class MarketDataTickReceived(Event):
    """A raw, parsed SPARK API `StockTickResult` push — see `infrastructure/yuanta/
    market_data_parsing.py`. Still vendor-symbol-addressed, not yet translated to
    `Instrument`/`ContractMonth`: `MarketDataBarService` (which already knows the
    currently active selection) does that translation and validates the symbol match,
    same division of responsibility as `BrokerSessionOrchestrator`'s existing "never
    translates a vendor symbol itself" boundary."""

    vendor_symbol: str
    price: Decimal
    size: int
    """This push's traded quantity (`DealVol`)."""
    serial_no: int
    """Per-symbol trade sequence number (`SerialNo`) — see `domain/tick.py`."""
    exchange_time: time
    """The bare exchange time-of-day from `StockTickResult.Time`, not yet
    date-resolved."""


@dataclass(frozen=True, slots=True)
class BarClosed(Event):
    """A 60-minute bar has closed — emitted exactly once, never revised. See
    `domain/bar_aggregator.py`."""

    instrument: Instrument
    contract: ContractMonth
    bar: Bar


@dataclass(frozen=True, slots=True)
class MarketDataFreshnessChanged(Event):
    instrument: Instrument
    contract: ContractMonth
    is_stale: bool


@dataclass(frozen=True, slots=True)
class MarketDataGapDetected(Event):
    """Published on every fresh `BrokerSessionReady` (first start or post-reconnect) —
    this codebase has no confirmed historical/tick-replay mechanism (see
    `docs/adr/0006-market-data-and-bar-aggregation.md`), so it can never know what
    happened before the first post-connect tick. Does not itself pause anything —
    Feature 05 (strategy signals) is expected to gate on this once it exists, matching
    `BrokerSessionInvalidated`'s existing "fires reliably, doesn't itself enforce" split.
    """

    instrument: Instrument
    contract: ContractMonth
    reason: str


@dataclass(frozen=True, slots=True)
class MarketDataGapCleared(Event):
    """The gap opened by the most recent `MarketDataGapDetected` for this contract has
    cleared — one bar closed cleanly end-to-end since then."""

    instrument: Instrument
    contract: ContractMonth


@dataclass(frozen=True, slots=True)
class BarPersistenceHealthChanged(Event):
    """The two-month bar-history write path (see `application.market_data.
    bar_service.MarketDataBarService`'s bounded write queue/retry) became degraded
    (a write could not be guaranteed after bounded retry, or the queue was full) or
    recovered. `desktop.composition.compute_readiness` surfaces `is_degraded` on the
    startup diagnostics screen — this event does not itself block anything."""

    is_degraded: bool


@dataclass(frozen=True, slots=True)
class BarRetentionCleanupCompleted(Event):
    """One two-month bar-history retention sweep finished — the "audit 摘要" the
    implementation prompt requires. Fired on `MarketDataBarService.start()` and again
    whenever a daily trading-day rollover is detected."""

    cutoff_trading_day: date
    deleted_count: int


@dataclass(frozen=True, slots=True)
class BarBackfillCompleted(Event):
    """One `BarHistoryBackfillService` run finished for `(instrument, contract)` — the
    "audit 摘要" for the vendor `GetKLine` backfill, symmetric with
    `BarRetentionCleanupCompleted`. `requested_day_count` is how many trading days in
    the rolling two-month window had no locally-recorded bar when this run started;
    `filled_day_count` is how many of those actually got at least one bar written this
    run (the rest remain gaps — a vendor rejection, timeout, or off-grid timestamp is
    never retried synchronously, only on the next trigger)."""

    instrument: Instrument
    contract: ContractMonth
    requested_day_count: int
    filled_day_count: int


@dataclass(frozen=True, slots=True)
class UnhandledHandlerError(Event):
    """Published by the EventCoordinator itself when a subscriber handler raises.

    Subscribing to this is how application code routes a bad handler toward a
    safe-pause / Faulted transition instead of losing the failure silently.
    """

    source_event: Event
    error: BaseException
