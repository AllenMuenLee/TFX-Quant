# ADR 0011 — Connectivity, reconnect, and safe pause (Feature 09)

## Status

Accepted.

## Context

`implementation prompt/09-connectivity-and-safe-pause/implementation-prompt.md` asks for
a connectivity health model spanning login, market data, trading, order/fill reports, and
query channels — tracked independently, never collapsed into one boolean — that
immediately stops new trading intents and safe-pauses on market-data staleness, report
interruption, trade-channel invalidation, query failure, or excessive clock skew; a
capped, jittered, cancellable reconnect; an ordered reconnect-reconciliation sequence;
"stay `Unknown`, never auto-resend" for orders unresolved across a disconnect; and a
mandatory human action before trading resumes.

Reading Features 06/07/08 first turned up a large, already-built surface this feature
sits on top of rather than duplicates: `OrderManager._on_session_ready` already calls
`reconcile_on_startup()` (requery + apply/​`UNKNOWN` every active order, never resend) on
every `BrokerSessionReady`; `PositionReconciliationService._on_session_ready` already
reconciles positions with `ReconciliationTrigger.RECONNECT`; `MarketDataBarService.
_on_session_ready` already reloads the persisted K-bar warm-up and raises
`MarketDataGapDetected`; `BarHistoryBackfillService._on_session_ready` already re-triggers
a K-line backfill. All four already subscribe to `BrokerSessionReady` and run
synchronously. What was genuinely missing: (1) any explicit per-channel health model at
all (only `application.ports.broker_session.SessionCapabilities`' five booleans and a
couple of per-contract staleness/gap flags existed); (2) anything that actually
*retries* — `BrokerSessionOrchestrator._invalidate_session` (Feature 02) moves the
session to `FAILED` on a passive post-ready disconnect and stops, with no auto-retry at
all; and (3) a coordinator that treats these signals as its own safe-pause job rather
than "fires reliably, doesn't enforce" (the split every prior feature explicitly left for
"Feature 09/10").

## Decisions

### 1. Five independently-tracked channels, plus a monitor-driven heartbeat distinct from
"last message"

`domain/connectivity.py`'s `ChannelId` mirrors `SessionCapabilities`' five flags
(login/market_data/trade/order_reports/queries) exactly, but each one gets its own
`ChannelHealth` (`connected`, `last_message_at`, `last_heartbeat_at`, `latency_ms`,
`last_error`, `is_stale`) rather than a single boolean — the implementation prompt's "不
以單一布林值代表所有連線" applied literally, one level more granular than
`SessionCapabilities` already was. Since no vendor-confirmed per-channel heartbeat push
exists (SPARK API's single unified session — see ADR 0004 — gives no reason to expect
one, and "不得臆測 API" forbids inventing one), `last_heartbeat_at` is defined honestly
as *this monitor's own* periodic health-evaluation timestamp
(`ConnectivityMonitor.on_clock_tick`, stamped on every channel every tick unconditionally)
— distinct from `last_message_at` ("the last time this channel produced real data").
`on_clock_tick` deliberately never touches `connected`/`is_stale`/`last_error` and never
publishes `ChannelHealthChanged` — a heartbeat tick alone must never look like recovery
for a channel that's actually still down (the "heartbeat 假陽性" acceptance scenario),
which the domain model makes structurally true rather than merely tested-for.

### 2. A new domain `ReconnectBackoffPolicy`, not a reuse of `infrastructure.yuanta.
backoff.BackoffPolicy`

`infrastructure/yuanta/backoff.py`'s `BackoffPolicy` is deliberately jitter-free — its own
docstring says so — because it only ever retries a single in-progress login attempt in
isolation. This feature's reconnect is a different, higher-level retry: after a
post-ready session goes bad, whether/when to call `IBrokerSession.start()` again at all.
The implementation prompt explicitly asks for jitter here ("避免登入風暴"), so
`domain/reconnect_backoff.py` adds `ReconnectBackoffPolicy` — same capped-exponential
shape, plus a `+/- jitter_ratio` perturbation, `random_fn` passed per-call (not fixed at
construction) so tests stay deterministic without monkeypatching `random`. Pure/stdlib,
consistent with `domain`'s existing "no I/O, no threading" layering rule — unlike
`BackoffPolicy`, it holds no `threading.Event`; cancellation is a plain field, since the
one caller (`ConnectivityMonitor`) already serializes access under its own lock.

### 3. `ConnectivityMonitor` is the one new coordinator, and it *is* the reconnect trigger
Feature 02 never built

`application/connectivity/connectivity_monitor.py`'s `ConnectivityMonitor` subscribes to
`BrokerCapabilitiesChanged`/`BrokerSessionInvalidated`/`BrokerLoggedOut`/
`BrokerLoginFailed`/`BrokerLoginTimedOut`/`MarketDataFreshnessChanged`/
`MarketDataTickReceived`/`OrderReportReceived`/`FillReceived`, maintains the five
`ChannelHealth` records, and — on `BrokerSessionInvalidated` — is the first code in this
codebase to actually retry: it remembers the operator's last `LoginRequest` (see decision
5) and calls `IBrokerSession.start()` again after each `ReconnectBackoffPolicy` delay,
observing `BrokerSessionReady`/`BrokerLoginFailed`/`BrokerLoginTimedOut` to decide
success/retry/exhaustion, `cancel_reconnect()` for the operator-facing "允許使用者停止".
`BrokerLoggedOut` (always user-initiated — the only publisher is `IBrokerSession.stop()`)
cancels any in-progress reconnect rather than letting it interfere with a deliberate
disconnect. Each retry attempt's own internal login flow still goes through
`BrokerSessionOrchestrator`'s existing jitter-free `BackoffPolicy` — the two backoffs are
layered (this feature's capped/jittered "should we try again at all" outer loop; Feature
02's existing "how hard to retry *this* login attempt" inner loop), not a replacement of
one by the other.

### 4. Query-failure and clock-skew detection observe existing calls; nothing calls the
broker an extra time

Adding a periodic health-check query of its own would double real broker traffic
(`PositionReconciliationService` already polls) and risk the documented vendor rate
limits (see `application.market_data.bar_history_backfill_service`'s `GetKLine`
comments). Instead, `application/connectivity/gateway_tracking.py`'s
`ConnectivityTrackingTradeGateway` implements `TradeGatewayPort` as a pure observer
wrapped around the real one: every `query_positions`/`query_order_reports`/`query_fills`/
`query_open_orders`/`submit_order`/`cancel_order` call already being made by
`OrderManager`/`PositionReconciliationService`/`ReversalWorkflowService`/`ScalingService`
is timed and its outcome (`ok`, `latency_ms`, `error`) reported to the monitor — no
additional calls, and every exception is always re-raised unchanged so those services'
own error handling is unaffected. A query failure immediately triggers
`SafePauseReason.QUERY_FAILED`. This is deliberately independent of, and does not
conflict with, `docs/adr/0010-position-reconciliation-and-manual-sync.md` decision 3 ("a
query failure is never treated as a *position discrepancy*") — that rule protects the
baseline-accounting logic specifically from over-reacting to one transient blip; this
feature's connectivity-level pause is a separate gate, and `attempt_safe_pause` is
already a no-op once `PAUSED_SAFE`, so the two never fight each other. Clock skew reuses
already-flowing broker-stamped timestamps — `OrderReport.at`/`Fill.at` (observed directly
off `OrderReportReceived`/`FillReceived`) and `Position.as_of` (observed off a successful
`query_positions` result) — compared against `Clock.now()` at receipt/observation time,
rather than inventing a dedicated vendor time-sync call no docs page confirms exists.

### 5. `ConnectivityTrackingBrokerSession` remembers the operator's `LoginRequest`; nothing
else in this codebase does

`IBrokerSession.start()` is only ever called from UI code
(`desktop/login_dialog.py`) that has the operator-entered credentials in hand — no
existing component keeps that request around afterward. Composition-time wiring can't
supply it either (it doesn't exist until the operator submits the form). So
`ConnectivityTrackingBrokerSession` wraps the real session purely to intercept `start()`
(remember the request) and `stop()` (forget it — an explicit disconnect must never be
followed by an automatic reconnect), delegating every other call unchanged.
`ConnectivityMonitor` itself is still built against the *raw*, un-wrapped session (its own
reconnect `start()` calls go straight to it) — `desktop/composition.py` constructs the
monitor first, then rebinds `broker_session`/`trade_gateway` to their tracking wrappers
for every other service, avoiding a construction cycle.

### 6. "Reconnect-reconciliation ran" is inferred from `EventCoordinator`'s documented
dispatch order, not a new completion event from four different services

`OrderManager.reconcile_on_startup()` and `PositionReconciliationService.
reconcile(RECONNECT)` don't publish a "reconciliation finished" event of their own — the
implementation prompt's "重連成功後...並進行 reconciliation" sequencing is satisfied by
those existing subscriptions, but this feature still needs to know *when* that fan-out
has finished for its own `SafePauseRecord.reconciled` audit flag. `EventCoordinator`
dispatches every handler for one event, in subscription order, on a single consumer
thread (its own docstring) — so `ConnectivityMonitor` splits its `BrokerSessionReady`
handling into two: `_on_session_ready_core` (subscribed unconditionally at construction —
resets channel health and reconnect-success bookkeeping, must never depend on wiring
order) and `_on_session_ready_reconciled` (subscribed only via the explicit
`attach_reconnect_reconciliation_watcher()` call, which `desktop/composition.py` makes
*last*, strictly after `OrderManager`/`PositionReconciliationService`/
`BarHistoryBackfillService` have all subscribed their own handlers). By the time the
reconciled-marking handler runs for a given `BrokerSessionReady`, every other
synchronous reconnect-reconciliation call for that same event has already completed. Bar
backfill (`BarHistoryBackfillService`) runs on its own background thread and is not
waited on — it already publishes its own `BarBackfillCompleted` with honest
partial/gap semantics; folding it into "reconciled" would either block on it (against its
own documented non-blocking design) or fake completion.

### 7. "重連前曾提交但未確定的委託一律保持 Unknown...不因 client timeout 自動重送" holds
structurally, by omission

Nothing in `ConnectivityMonitor`/its gateway wrappers ever calls `OrderManager.submit()`
or `TradeGatewayPort.submit_order()` — reconnect only ever calls `IBrokerSession.start()`.
`OrderManager`'s existing (Feature 06) timeout sweep and `reconcile_on_startup()` already
guarantee an order left unresolved across a disconnect is only ever settled by a matching
broker report/fill or explicitly marked `UNKNOWN` — never resent. Two integration tests
(`tests/application/connectivity/test_reconnect_integration.py`) exercise this
end-to-end through the actual reconnect path rather than a bare `BrokerSessionReady`
publish: a fill that only arrives via the reconciliation query (never live) resolves the
order to `FILLED` with exactly one `submit_order` call ever recorded; a reconciliation
query that flatly contradicts the locally-believed status (an illegal order-report
transition per `domain.order_state_machine`) resolves to `UNKNOWN`, again with no resend.

### 8. First-trigger-wins, reusing ADR 0010's exact "only escalate from `RUNNING`" gate

`ConnectivityMonitor._trigger_pause` only ever calls `attempt_safe_pause` when
`StrategyStateMachine.state is RUNNING` — identical reasoning and gate to ADR 0010
decision 2 (repeatedly detecting the *same still-unresolved* problem, or a second,
different problem, while already `PAUSED_SAFE` must never progressively escalate toward
`FAULTED`, and must never overwrite the first-recorded `SafePauseRecord`). Before the
strategy has ever run (`STOPPED`), every trigger is correctly a no-op too, since
`StartupSafetyGate` is the only path into `RUNNING` in the first place.

### 9. `TRADE_CHANNEL_INVALID`/`ORDER_REPORTS_INTERRUPTED` are independently modeled even
though SPARK API currently makes them co-vary

Under ADR 0004's single unified session, a passive disconnect collapses `trading` and
`order_reports` capabilities together, and `BrokerSessionInvalidated` fires before
`BrokerCapabilitiesChanged` for the same event — so in practice today the first pause
reason recorded is always `TRADE_CHANNEL_INVALID` (from `_on_session_invalidated`), and
`_on_capabilities_changed`'s independent regression checks for both capabilities become
no-ops once already paused. They stay separately implemented anyway — per "不以單一布林
值代表所有連線" and so a future, vendor-confirmed genuinely-independent failure mode
(e.g. reports interrupted without trading itself failing) needs no redesign.

### 10. No new persistence; a minimal, read-only `ConnectivityPanel` plus the one required
operator control

Channel health and the current `SafePauseRecord` are in-memory + structured-logged only,
same posture as `MarketDataBarService`'s `is_stale`/`has_gap` (no SQLite table for
connectivity state — it's runtime/session state, not an audit trail requiring durability
across restarts; the structured logs are the durable audit trail). `desktop/
connectivity_panel.py` (embedded in `ReadinessFrame`, same pattern as `MarketDataPanel`)
displays each channel's health and the pause reason/detected/effective time, and exposes
`cancel_reconnect()` behind a "停止重連" button — the implementation prompt's one
explicit operator action ("允許使用者停止"). No "press continue" control exists: exactly
like every prior feature (see ADR 0008/0009/0010's identically-worded gaps), nothing in
this codebase yet transitions `StrategyState` into `STARTING`/`RUNNING` at all —
`StartupSafetyGate` is the only path, and no strategy-engine/order-entry UI (Feature
05/12) exists to drive it. `compute_readiness` gains one new row ("Connectivity: no
unresolved safe-pause") that checks `SafePauseRecord.reconciled`, not "a pause ever
happened this session" — so it correctly turns green again once reconnect-reconciliation
finishes, without implying (or requiring) that `StrategyState` has left `PAUSED_SAFE`.

## Consequences

- `desktop/composition.py`'s wiring order is now load-bearing in two ways, both commented
  in place: `ConnectivityMonitor` must be constructed with the *raw* `broker_session`
  before `trade_gateway`/`broker_session` are rebound to their tracking wrappers, and
  `connectivity_monitor.attach_reconnect_reconciliation_watcher()` must be called after
  `bar_history_backfill_service`/`reconciliation_service`/`order_manager` have all
  subscribed their own `BrokerSessionReady` handlers.
- `MockBrokerSession` gains `script_start_failures()`/`start_calls` (mirrors Feature
  07/08's `set_positions()`/`fail_next_query_positions()` mock-scripting precedent) so
  reconnect retry/exhaustion can be tested without a real vendor connection.
- `application.connectivity.connectivity_monitor.Scheduler` duplicates
  `infrastructure.yuanta.session_orchestrator.Scheduler`'s shape rather than importing it
  — `application` code must not depend on `infrastructure` (import-linter contract).
- Every currently-known trigger for `TRADE_CHANNEL_INVALID`/`ORDER_REPORTS_INTERRUPTED`
  reduces to the same underlying SPARK API session event (decision 9) — genuinely
  independent triggers for those two reasons remain untested against a real vendor
  connection, honestly matching this codebase's existing "ports-and-mock-now" posture for
  everything not yet confirmed against live SPARK API docs/behavior.
- As with every prior feature, reaching `PAUSED_SAFE`/completing reconnect-reconciliation
  never itself resumes trading — `on_strategy_start()`-style "no automatic caller yet"
  gaps (Feature 05/12's job) are unchanged by this feature.
