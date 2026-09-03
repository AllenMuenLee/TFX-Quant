"""Internal event shapes.

Broker callbacks (COM events, arriving on whatever thread the OCX message pump runs
on) are translated into one of these immutable event objects *before* being handed to
the `EventCoordinator` — nothing downstream of the coordinator ever touches a raw
vendor callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tfx_quant.application.ports.broker_session import LogoutReason, SessionCapabilities
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.connectivity import ChannelHealth, ChannelId, SafePauseRecord
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderReport, OrderStatus
from tfx_quant.domain.position_reconciliation import DiscrepancyKind, ReconciliationTrigger
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reversal_workflow import (
    FlatConfirmationResult,
    ReversalWorkflowId,
    ReversalWorkflowState,
)
from tfx_quant.domain.risk import EodFlattenTrigger, EodFlattenWorkflowId, EodFlattenWorkflowState
from tfx_quant.domain.strategy_state import StrategyState
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
    """One broker order-level report (ack/reject/cancel-confirm/...) — see
    `domain.order_state_machine.OrderReport`. Consumed exclusively by
    `application.order_management.order_manager.OrderManager`."""

    report: OrderReport


@dataclass(frozen=True, slots=True)
class FillReceived(Event):
    """One broker fill report. Consumed exclusively by `OrderManager`."""

    fill: Fill


@dataclass(frozen=True, slots=True)
class OrderStateTransitioned(Event):
    """One local `OrderIntent` moved from one `OrderStatus` to another — published after
    every successfully applied order report or fill (never for a duplicate/out-of-order
    report that was ignored). Available for future features (position reconciliation,
    UI) to subscribe to without depending on `OrderManager` directly."""

    local_order_id: LocalOrderId
    client_order_id: ClientOrderId
    from_status: OrderStatus
    to_status: OrderStatus
    trigger: str
    broker_order_no: str | None


@dataclass(frozen=True, slots=True)
class OrderRequiresManualReview(Event):
    """An order reached `REJECTED`, `UNKNOWN`, or an otherwise undecidable state — the
    safe-pause hook. Fires reliably; does not itself enforce a strategy-wide pause, same
    split as `BrokerSessionInvalidated`/`MarketDataGapDetected` — gating `StrategyState`
    on this is Feature 09/10's job."""

    local_order_id: LocalOrderId
    client_order_id: ClientOrderId
    status: OrderStatus
    reason: str


@dataclass(frozen=True, slots=True)
class TradeLedgerFillRecorded(Event):
    """`application.trade_reports.fill_ledger_service.FillLedgerService` translated a
    `FillReceived` into an append-only `LedgerFill` and handed it to the ledger. Carries
    the dedup `outcome` (`FillAppendOutcome` value) so a duplicate broker fill callback is
    visibly a no-op, and `simulation` so a UI/subscriber can key on provenance without
    reloading the row."""

    fill_id: str
    outcome: str
    order_correlation: str
    simulation: bool


@dataclass(frozen=True, slots=True)
class TradeLedgerAppendFailed(Event):
    """A `FillReceived` could not be turned into a `LedgerFill` — no matching order
    intent, the intent has no broker order number yet, or the ledger write itself failed.
    Fires reliably; does not itself enforce a strategy-wide pause, same "fires reliably,
    doesn't enforce" split as `OrderRequiresManualReview` — the P&L numbers are simply
    incomplete until an operator resolves it."""

    client_order_id: ClientOrderId
    reason: str


@dataclass(frozen=True, slots=True)
class BrokerLoginSucceeded(Event):
    """The Yuanta order OCX login callback reported success.

    `accounts` is the parsed, futures-only account list — session-ready still
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
    """Login, order query, fill query, and position query all succeeded — every
    `SessionCapabilities` flag is now true. Yuanta quote-API readiness is a wholly
    separate concern — see `desktop.composition.compute_readiness` — and is not part
    of this event or `SessionCapabilities`."""

    account: TradingAccount


@dataclass(frozen=True, slots=True)
class InstrumentSwitchCompleted(Event):
    """`InstrumentSelectionService.switch_to()` succeeded — account status requeried,
    bar/signal state cleared, and `vendor_symbol` carried here for
    `desktop.quote_runtime.QuoteRuntime._on_switch`. That handler changes only which
    recorded market is charted: every `Instrument` is registered and recorded for the
    whole quote session, so a switch re-registers nothing unless it also changes a
    contract month. The strategy state machine is left in whatever paused/stopped state
    it was already in; per Feature 03's acceptance criteria the user must still press
    start again (Running is only ever reachable via a fresh Starting)."""

    instrument: Instrument
    contract: ContractMonth
    vendor_symbol: str


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
class LatestPriceObserved(Event):
    """The most recent valid match price for a recorded market, published by
    `desktop.quote_runtime.QuoteRuntime` — coalesced to at most one per second per
    (instrument, contract) so it is a mark-to-market feed, not a per-tick firehose.
    `quality` (`"OK"` / `"STALE"` / `"GAP"`) lets a consumer refuse to value a position
    off a price it cannot trust rather than silently using a stale number."""

    instrument: Instrument
    contract: ContractMonth
    price: Decimal
    observed_at: Timestamp
    quality: str


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
class ReversalWorkflowStarted(Event):
    """A reversal workflow began (`ReversalWorkflowState.STARTED` persisted) — the
    "workflow ID、起始實際持倉、目標方向" debug-log requirement's event counterpart.
    Published once per `trigger_key`, never on a deduped resubmission."""

    workflow_id: ReversalWorkflowId
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    trigger_key: str


@dataclass(frozen=True, slots=True)
class ReversalFlatConfirmed(Event):
    """The flat-confirmation gate passed — a fresh broker position query independently
    corroborated the close order's full-fill report. The reverse entry order is
    submitted only after this, never before — see `ReversalEntrySubmitted.at`."""

    workflow_id: ReversalWorkflowId


@dataclass(frozen=True, slots=True)
class ReverseEntryBlocked(Event):
    """The reverse entry order was *not* submitted — either the flat-confirmation gate
    failed (`result` populated) or the workflow was blocked before ever querying a
    position (too close to the mandatory EOD flatten deadline, or already flat with
    nothing to reverse — `result` is `None` in that case). The explicit "在 flat 未確認
    前應有明確的 reverse_entry_blocked 事件" requirement."""

    workflow_id: ReversalWorkflowId
    reason: str
    result: FlatConfirmationResult | None


@dataclass(frozen=True, slots=True)
class ReversalEntrySubmitted(Event):
    """The reverse entry order was submitted — always strictly after
    `ReversalFlatConfirmed` for the same `workflow_id`. The acceptance test's "反向單
    時間戳一定晚於全平成交及零持倉查詢" assertion anchors on this event's `at`."""

    workflow_id: ReversalWorkflowId
    entry_client_order_id: ClientOrderId


@dataclass(frozen=True, slots=True)
class ReversalCompleted(Event):
    """The reverse entry order reached `OrderStatus.FILLED` — the workflow is done."""

    workflow_id: ReversalWorkflowId


@dataclass(frozen=True, slots=True)
class ReversalPausedSafe(Event):
    """A reversal workflow moved to `ReversalWorkflowState.PAUSED_SAFE` — partial fill,
    reject, timeout, disconnect, or a contradictory query at any step. Fires reliably;
    does not itself force a `StrategyState` transition, same "fires reliably, doesn't
    enforce" split as `BrokerSessionInvalidated`/`OrderRequiresManualReview`. Nothing in
    this codebase auto-resumes a paused workflow."""

    workflow_id: ReversalWorkflowId
    state: ReversalWorkflowState
    reason: str


@dataclass(frozen=True, slots=True)
class PositionDiscrepancyDetected(Event):
    """Broker-actual position diverged from this system's own expected baseline in
    direction or quantity — `application.position_reconciliation.
    PositionReconciliationService.reconcile()` found a `DiscrepancyKind != NONE`.

    Unlike `BrokerSessionInvalidated`/`OrderRequiresManualReview`/`ReversalPausedSafe`
    ("fires reliably, does not itself enforce a strategy-wide pause — gating
    `StrategyState` is Feature 09/10's job"), this event's publisher *does* drive
    `StrategyStateMachine` toward `PAUSED_SAFE`/`FAULTED` itself before publishing —
    position reconciliation is explicitly this feature's own job per the implementation
    prompt, not deferred."""

    trigger: ReconciliationTrigger
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    expected_net: NetPosition
    actual_net: NetPosition
    discrepancy: DiscrepancyKind
    resulting_strategy_state: StrategyState | None
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ManualPositionSyncCompleted(Event):
    """A human confirmed `PositionReconciliationService.confirm_manual_sync()` — the
    expected baseline now equals the broker-confirmed actual position. The strategy
    remains wherever it already was; this event never itself resumes `RUNNING`."""

    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    baseline_before: NetPosition
    baseline_after: NetPosition
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ChannelHealthChanged(Event):
    """One connectivity channel's connected/stale/error status changed — published only
    on a meaningful change (never per-message/per-tick), same "publish on change only"
    convention as `BrokerCapabilitiesChanged`/`MarketDataFreshnessChanged`. See
    `application.connectivity.connectivity_monitor.ConnectivityMonitor`."""

    channel: ChannelId
    health: ChannelHealth


@dataclass(frozen=True, slots=True)
class SafePauseTriggered(Event):
    """A new connectivity safe-pause episode began — see `domain.connectivity.
    SafePauseRecord`. Unlike `BrokerSessionInvalidated`/`MarketDataFreshnessChanged`/
    `OrderRequiresManualReview`/`ReversalPausedSafe` ("fires reliably, does not itself
    enforce a strategy-wide pause"), this event's publisher *does* drive
    `StrategyStateMachine` toward `PAUSED_SAFE`/`FAULTED` itself before publishing —
    same "this feature's own job, not deferred" split as `PositionDiscrepancyDetected`."""

    record: SafePauseRecord


@dataclass(frozen=True, slots=True)
class ConnectivityReconciled(Event):
    """A fresh `BrokerSessionReady` was observed after a safe-pause episode, and the
    synchronous reconnect-reconciliation fan-out (`OrderManager.reconcile_on_startup`,
    `PositionReconciliationService.reconcile(RECONNECT)`) has run — see
    `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`'s subscription-order note.
    Never itself resumes `RUNNING`."""

    record: SafePauseRecord


@dataclass(frozen=True, slots=True)
class EodFlattenWorkflowStarted(Event):
    """A Feature 10 flatten workflow began (`EodFlattenWorkflowState.STARTED`
    persisted) — the mandatory 04:55 forced flatten or an operator-confirmed emergency
    flatten. Published once per `trigger_key`, never on a deduped resubmission."""

    workflow_id: EodFlattenWorkflowId
    trigger: EodFlattenTrigger
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    trigger_key: str


@dataclass(frozen=True, slots=True)
class EodFlattenCompleted(Event):
    """A flatten workflow reached `EodFlattenWorkflowState.COMPLETED` — a fresh broker
    position query independently confirmed exactly zero after the close order's fill
    report, never inferred from the fill report alone."""

    workflow_id: EodFlattenWorkflowId


@dataclass(frozen=True, slots=True)
class EodFlattenPausedSafe(Event):
    """A flatten workflow moved to `EodFlattenWorkflowState.PAUSED_SAFE` — a reject,
    unexpected cancel, timeout/`UNKNOWN`, disconnect, or a contradictory final position
    query. Fires reliably; never auto-resumes, same "PAUSED_SAFE is terminal" philosophy
    as `ReversalPausedSafe`. This is the highest-priority alert in the codebase — it
    means the mandatory 04:55 (or emergency) flatten did *not* complete and a position
    may still be open."""

    workflow_id: EodFlattenWorkflowId
    state: EodFlattenWorkflowState
    reason: str


@dataclass(frozen=True, slots=True)
class StartupPositionSafetyPauseTriggered(Event):
    """The process started (or reconnected for the first time) already inside the
    04:55-10:45 no-entry band with a non-zero broker-confirmed position —
    `RiskSupervisor` never auto-flattens this case; it only forces a safe pause and
    requires the operator to use the emergency-flatten control. See the implementation
    prompt's "若程式在 04:55 之後啟動且有持倉，保持安全暫停並提示人工執行緊急平倉"."""

    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    net: NetPosition
    resulting_strategy_state: str | None


@dataclass(frozen=True, slots=True)
class UnhandledHandlerError(Event):
    """Published by the EventCoordinator itself when a subscriber handler raises.

    Subscribing to this is how application code routes a bad handler toward a
    safe-pause / Faulted transition instead of losing the failure silently.
    """

    source_event: Event
    error: BaseException
