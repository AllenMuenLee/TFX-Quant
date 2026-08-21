# Modules

What's in each package under `src/tfx_quant/`. Dependency direction points toward
`domain`; see `docs/adr/0003-layering-and-event-threading-model.md` for how that's
enforced (`import-linter`).

## `domain`

Immutable value types and pure business rules. Stdlib only — no third-party
dependency, no I/O. `Instrument`, `ContractMonth`, `InstrumentMasterEntry` (Feature 03
— 商品主檔), `TradingAccount`, `Side`, `Quantity`/`NetPosition`, `Price`/`Money`,
`Timestamp`, `Bar` (+ `CandleColor` — Feature 04), `Tick`, `TradingCalendar` (+
`session_context_for` — the two-month-history extension's trading-day/session
resolver), `BarAggregator`/`CandleStreakCounter` (Feature 04 — 60-minute bar aggregation
and red/black/doji streak counting; see `docs/adr/0006-market-data-and-bar-
aggregation.md`), `BarRecord`/`BarPeriod`/`MarketSession`/`BarDataSource`/
`rolling_two_month_start`/`ContinuitySegment`/`continuous_segments`/`BarConflictAudit`
(`bar_record.py` — the two-month bar-history extension's persisted-record shape,
rolling-window math, and gap-detection; `BarDataSource` has two members,
`AGGREGATED_FROM_YUANTA_REALTIME` and `BACKFILLED_FROM_YFINANCE`; `BarConflictAudit`
(existing/incoming `BarRecord` + detected-at) is the yfinance-backfill extension's
local-vs-third-party conflict audit shape; see `docs/adr/0007-two-month-bar-history-
persistence.md`), `chunk_consecutive_days` (`bar_history_backfill.py` — pure date-range
chunking for the yfinance backfill's bounded batch queries, vendor-neutral by design),
`StrategySignal`, `Position`, `Order`/`ClientOrderId`, `Fill` (+ `broker_fill_no`/
`broker_seq_no` — Feature 06), `Pnl`, `StrategyState` + `StrategyStateMachine`,
`OrderStatus`/`LocalOrderId`/`OrderReport`/`OrderIntent`/`OrderStateMachine`/
`worst_case_net_position_range` (`order_state_machine.py` — Feature 06's order/fill
lifecycle state model; see `docs/adr/0008-order-and-fill-state-machine.md`),
`ReversalWorkflowState`/`ReversalWorkflowId`/`ReversalWorkflowRecord`/
`ReversalWorkflowStateMachine`/`FlatConfirmationResult`/`reversal_side_for`
(`reversal_workflow.py` — Feature 07's multi-step reversal lifecycle state model; see
`docs/adr/0009-safe-reversal-and-scaling.md`; the state machine gained one narrowly-
scoped extra legal edge, `PAUSED_SAFE -> BLOCKED`, for Feature 08's manual-sync reset —
see below), `attempt_safe_pause` (`strategy_state.py` — the shared "PAUSED_SAFE when
reachable, else FAULTED, else leave alone" helper `desktop.__main__`'s uncaught-
exception handler and Feature 08's `PositionReconciliationService` both use),
`ReconciliationTrigger`/`DiscrepancyKind`/`classify_discrepancy`/
`SUSPECTED_CAUSE_HYPOTHESES`/`PositionBaseline`/`ReconciliationRecord`/
`ManualSyncPreflight`/`ManualSyncRecord` (`position_reconciliation.py` — Feature 08's
expected-vs-actual-position comparison model and manual-sync gate/audit shapes; see
`docs/adr/0010-position-reconciliation-and-manual-sync.md`), `ChannelId`/`ChannelHealth`/
`SafePauseReason`/`SafePauseRecord`/`clock_skew_seconds` (`connectivity.py` — Feature 09's
per-channel connectivity health snapshot and the connectivity safe-pause audit record;
see `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`), `ReconnectBackoffPolicy`
(`reconnect_backoff.py` — Feature 09's capped-exponential-with-jitter reconnect backoff,
deliberately not a reuse of `infrastructure.yuanta.backoff.BackoffPolicy`, which is
jitter-free by design — see the same ADR). All illegal-state construction raises a
`DomainError` subclass from `domain/errors.py`.

## `application`

- `application/ports/` — `Protocol` interfaces (`Clock`, `IdGenerator`,
  `TradeGatewayPort`, `QuoteGatewayPort`, `IBrokerSession`, `InstrumentMasterRepository`,
  `BarSignalStateStore`, `TradingCalendarRepository`, `BarRecordRepository`,
  `OrderRepository`, `ReversalWorkflowRepository`, `PositionBaselineRepository`,
  `YahooTickerMappingRepository`, `YahooHistoryQueryPort`) that infrastructure/persistence
  implementations satisfy.
  `IBrokerSession` (Feature 02) is the Yuanta login/session-lifecycle port — richer than,
  and additive to, `TradeGatewayPort`/`QuoteGatewayPort`; see `docs/adr/0004-broker-session-
  architecture.md`. `InstrumentMasterRepository`/`BarSignalStateStore` (Feature 03) are
  the 商品主檔 lookup and K棒/訊號 reset seams — see `docs/adr/0005-instrument-master-
  and-selection.md`. `TradingCalendarRepository` (Feature 04) is the controlled 交易日曆
  （休市／提早收盤）lookup seam — see `docs/adr/0006-market-data-and-bar-
  aggregation.md`. `BarRecordRepository` (the two-month bar-history extension) is the
  closed-bar persistence seam `SqliteBarRecordRepository` implements — see
  `docs/adr/0007-two-month-bar-history-persistence.md`; the yfinance-backfill revision of
  that same extension adds `get_one`/`record_conflict`/`list_conflicted_trading_days` to
  it. `YahooTickerMappingRepository` (內部商品／契約 -> Yahoo ticker lookup, backed by
  `JsonYahooTickerMappingRepository`) and `YahooHistoryQueryPort` (`YahooBar`/
  `YahooHistoryQueryError`, backed by `YfinanceHistoryQueryAdapter`/
  `MockYahooHistoryQuery`) are that same revision's two new ports — see the same ADR's
  2026-08-21 extension section. `OrderRepository` (Feature 06) is
  the order-intent persistence seam `SqliteOrderRepository` implements, plus
  `TradeGatewayPort`'s new `submit_order`/`cancel_order`/`query_order_reports`/
  `query_fills` methods — see `docs/adr/0008-order-and-fill-state-machine.md`.
  `ReversalWorkflowRepository` (Feature 07) is the reversal-workflow persistence seam
  `SqliteReversalWorkflowRepository` implements — see `docs/adr/0009-safe-reversal-and-
  scaling.md`. `PositionBaselineRepository` (Feature 08) is the expected-position-
  baseline persistence seam `SqlitePositionBaselineRepository` implements — see
  `docs/adr/0010-position-reconciliation-and-manual-sync.md`.
- `application/events/` — `Event` shapes and `EventCoordinator`, the single
  serialized event-processing queue. Feature 04 adds `MarketDataTickReceived`,
  `BarClosed`, `MarketDataFreshnessChanged`, `MarketDataGapDetected`/
  `MarketDataGapCleared`; the two-month bar-history extension adds
  `BarPersistenceHealthChanged`/`BarRetentionCleanupCompleted`, and its yfinance-backfill
  revision adds `BarBackfillCompleted` (instrument/contract + requested/filled/conflict
  bar counts — the yfinance run's own "audit 摘要"); Feature 06 reshapes
  `OrderReportReceived` (now carries `OrderReport`, not `Order`) and adds
  `OrderStateTransitioned`/`OrderRequiresManualReview`; Feature 07 adds
  `ReversalWorkflowStarted`/`ReversalFlatConfirmed`/`ReverseEntryBlocked`/
  `ReversalEntrySubmitted`/`ReversalCompleted`/`ReversalPausedSafe` (no new scaling
  events — logging-only, see `docs/adr/0009-safe-reversal-and-scaling.md`); Feature 08
  adds `PositionDiscrepancyDetected`/`ManualPositionSyncCompleted` — the former is the
  one event in this codebase whose publisher *also* directly enforces the
  `StrategyState` pause itself, rather than "fires reliably, doesn't enforce" (see
  `docs/adr/0010-position-reconciliation-and-manual-sync.md`). Feature 09 adds
  `ChannelHealthChanged` (published only on a meaningful per-channel change, never
  per-message/per-tick), `SafePauseTriggered` (the second event, after
  `PositionDiscrepancyDetected`, whose publisher — `ConnectivityMonitor` — also directly
  enforces the `StrategyState` pause itself), and `ConnectivityReconciled` (published once
  a fresh `BrokerSessionReady` following a pause has let the existing reconnect-
  reconciliation fan-out run — see `docs/adr/0011-connectivity-reconnect-and-safe-
  pause.md`).
- `application/safety/` — `SafetyChecklist` + `StartupSafetyGate`, the only path into
  `StrategyState.RUNNING`. Nine independent checklist items as of Feature 03.
- `application/settings/` — `TradingSettings` (pydantic) + `validate_startup()`.
- `application/instrument_selection/` (Feature 03) — `InstrumentSelectionService` (the
  switch/selection workflow), `ResolvedSelection`, and pure validation helpers
  (`validate_can_open`, `validate_order_price`,
  `check_quote_position_order_consistent`) that Feature 06's order submission and the
  startup safety checklist both reuse.
- `application/market_data/` (Feature 04) — `MarketDataBarService`: the real
  `BarSignalStateStore` implementation, and the tick→bar pipeline. Subscribes to
  `MarketDataTickReceived`/`BrokerSessionReady`, drives a `domain.BarAggregator` per
  active contract, republishes `BarClosed`/staleness/gap events, and exposes the
  forming-bar/recent-bars/stale/gap query surface the desktop UI reads. See
  `docs/adr/0006-market-data-and-bar-aggregation.md`. The two-month bar-history
  extension adds: a bounded-queue/background-writer path that persists every closed bar
  via `BarRecordRepository` without blocking tick processing; `_load_warm_up()` (called
  from both `clear()` and `_on_session_ready()`) that restores the streak counter and
  recent-bars view from persisted history on startup/reconnect/switch; startup + daily
  retention cleanup; and the `continuous_warm_up_bars`/`recorded_range`/`query_history`/
  `is_persistence_degraded`/`candle_streak` query methods — `continuous_warm_up_bars` also
  excludes any trading day with an unresolved yfinance conflict (see below). See
  `docs/adr/0007-two-month-bar-history-persistence.md`. `bar_history_backfill_service.py`
  (the same extension's yfinance-backfill revision) adds `BarHistoryBackfillService`: a
  second, independent writer that computes the rolling window's missing canonical bar
  identities (bar-level, not day-level — see the ADR's 2026-08-21 decision 11), queries
  `YahooHistoryQueryPort` in bounded, padded date-range chunks, aligns/validates each
  returned bar against `TradingCalendar.boundary_for_open`, and upserts via
  `BarRecordRepository` — same "never blocks the `EventCoordinator` dispatch thread, runs
  on its own background thread" shape as the (now-superseded) vendor `GetKLine` path it
  replaced. Triggers on `BrokerSessionReady`/`InstrumentSwitchCompleted` plus its own
  `start()`/`stop()`-owned daily-rollover timer; exposes `is_degraded()` (missing ticker
  mapping or an unresolved conflict for the active contract) for the readiness screen.
- `application/order_management/` (Feature 06) — `OrderManager`, the only component
  allowed to turn a trading intent into a real Yuanta order, plus its `OrderRequest`
  input DTO, `validation.validate_exposure_within_cap`, and its own `errors.py`
  (`ActiveWorkflowInProgressError`/`OrderExposureExceededError`/`OrderNotFoundError`).
  Subscribes to `OrderReportReceived`/`FillReceived`/`BrokerSessionReady`; persists every
  transition via `OrderRepository` synchronously (not the async bounded-queue pattern
  `MarketDataBarService` uses for bars — order volume is low and the point is a
  *stronger* durability guarantee). See `docs/adr/0008-order-and-fill-state-machine.md`.
- `application/reversal_scaling/` (Feature 07) — `ReversalWorkflowService` (the
  persisted, recoverable reversal state machine driver — `start_reversal`/
  `resume_pending_workflows`, both funneling through the same `_advance` step-dispatcher)
  and `ScalingService` (single-step ±1→±2 add-on evaluator/submitter, no persisted
  workflow of its own), `gates.py` (`evaluate_flat_confirmation`/`evaluate_scaling_gate`/
  `is_too_close_to_eod`), and `errors.py`
  (`ReversalAlreadyActiveError`/`InvalidSignalKindError`). Both services only ever send
  an order through `OrderManager` — neither ever calls `TradeGatewayPort.submit_order`
  directly. See `docs/adr/0009-safe-reversal-and-scaling.md`.
- `application/position_reconciliation/` (Feature 08) — `PositionReconciliationService`:
  queries `TradeGatewayPort.query_positions()` at every required trigger (login,
  reconnect, every fill, the reversal flat-confirmation gate, a timed poll, foreground
  return, and manual requery), compares against the persisted `PositionBaseline`, and
  drives `StrategyStateMachine` toward `PAUSED_SAFE`/`FAULTED` itself on any mismatch
  (the one exception to this codebase's usual "event fires, doesn't enforce" split).
  `expected_net_lookup` replaces Feature 06's always-flat `OrderManager.position_lookup`
  placeholder. `confirm_manual_sync()` is the two-step manual-sync flow's second half —
  re-verifies the broker position fresh, gates on no active/unknown local orders,
  updates the baseline, resets bar/signal state via `BarSignalStateStore.clear()`, and
  retires (never resumes) any `PAUSED_SAFE` reversal workflow on that contract — and
  never itself submits an order or resumes `RUNNING`. `errors.py`
  (`ManualSyncBlockedError`/`StaleSyncConfirmationError`). See `docs/adr/0010-position-
  reconciliation-and-manual-sync.md`.
- `application/connectivity/` (Feature 09) — `connectivity_monitor.ConnectivityMonitor`:
  tracks the five `domain.connectivity.ChannelHealth` records, drives
  `StrategyStateMachine` toward `PAUSED_SAFE`/`FAULTED` itself on market-data staleness,
  a trade/order-reports capability regression, a query failure, or excessive clock skew
  (the third exception, after `PositionDiscrepancyDetected`/`ReversalPausedSafe`'s "fires
  reliably" siblings, to this codebase's "event fires, doesn't enforce" split), and is
  the reconnect coordinator: capped/jittered retry of `IBrokerSession.start()` after a
  passive post-ready disconnect (`domain.reconnect_backoff.ReconnectBackoffPolicy`),
  cancellable, remembering the operator's `LoginRequest` via
  `gateway_tracking.ConnectivityTrackingBrokerSession`. Its `BrokerSessionReady` handling
  is deliberately split in two — `_on_session_ready_core` (subscribed unconditionally)
  vs. `_on_session_ready_reconciled` (subscribed only via the explicit
  `attach_reconnect_reconciliation_watcher()` call `desktop/composition.py` makes last) —
  so the "reconnect-reconciliation fan-out already ran" audit flag relies on
  `EventCoordinator`'s documented subscription-order dispatch guarantee rather than a new
  completion event from `OrderManager`/`PositionReconciliationService`.
  `gateway_tracking.ConnectivityTrackingTradeGateway` implements `TradeGatewayPort` as a
  pure observer wrapped around the real one — every query/submit/cancel call other
  services already make is timed and its outcome reported to the monitor, never an
  additional broker call, never a swallowed exception. See `docs/adr/0011-connectivity-
  reconnect-and-safe-pause.md`.

Depends only on `domain`.

## `infrastructure`

Real and mock implementations of `application.ports`. `infrastructure/clock.py`
(`SystemClock`), `infrastructure/identity.py` (`UuidIdGenerator`), and
`infrastructure/bar_signal_state.py` (`NullBarSignalStateStore`/
`InMemoryBarSignalStateStore` — Feature 03 test/dev placeholders; the real bar/signal
state is `application.market_data.MarketDataBarService`, Feature 04) are generic.
`infrastructure/market_data/` (Feature 04) — `JsonTradingCalendarRepository`, backed by
`trading_calendar.example.json` (a best-effort, web-search-seeded 2026 TAIFEX holiday
list, explicitly flagged unconfirmed); generic, not Yuanta-specific, since exchange
holidays aren't vendor data. The two-month bar-history extension's yfinance-backfill
revision adds three more files to this same package:
`yahoo_ticker_mapping_repository.py` (`JsonYahooTickerMappingRepository`, backed by
`yahoo_ticker_mapping.example.json` — deliberately shipped with an empty `mappings`
array, since no Yahoo Finance ticker for any TAIFEX futures contract has been confirmed;
see `docs/adr/0007-two-month-bar-history-persistence.md`'s 2026-08-21 extension);
`yfinance_history_adapter.py` (`YfinanceHistoryQueryAdapter`, the real
`YahooHistoryQueryPort` — the **only** module allowed to `import yfinance`/`import
pandas`, isolated the same way `infrastructure/yuanta/` isolates vendor types, imported
lazily by `desktop/composition.py` only in the real, non-mock branch); and
`mock_yahoo_history_query.py` (`MockYahooHistoryQuery`, always returns an empty result —
mock mode has no network access, so every range simply stays a gap). `infrastructure/
yuanta/` is the Yuanta-specific adapter —
the **only** package allowed to import vendor (`pythonnet`/`YuantaOneAPI`) types (its
`instrument_master_repository.py`, added by Feature 03, and `market_data_parsing.py`,
added by Feature 04, are exceptions to that part only — plain parsing/JSON I/O, no
`pythonnet` dependency, but Yuanta-specific data/wire-format). Feature 01 shipped
`MockTradeGateway`/`MockQuoteGateway` only; Feature 02 adds the real session
(`session_orchestrator.py`, `spark_client.py`, `spark_api_adapter.py`, `credentials.py`,
`preflight.py`, `backoff.py`) plus `MockBrokerSession`; Feature 03 adds
`instrument_master_repository.py` (`JsonInstrumentMasterRepository`, backed by
`instrument_master.example.json`, whose `vendor_symbol` values are now
`domain.instrument_master.futures_quote_symbol()`'s real, formula-computed output — see
ADR 0005's addendum) and implements `broker_session_gateway_views.py`'s
previously-stubbed `subscribe`/`unsubscribe`; Feature 04 adds `market_data_parsing.py`
(`StockTickResult` field parsing) and wires it through `spark_api_adapter.py`'s
`OnResponse` dispatch and `session_orchestrator.py`'s `handle_market_data_push`. See
`infrastructure/yuanta/README.md` for the vendor API inventory,
`docs/adr/0004-broker-session-architecture.md` for the session architecture,
`docs/adr/0005-instrument-master-and-selection.md` for the instrument master/selection
design, and `docs/adr/0006-market-data-and-bar-aggregation.md` for the market-data/bar
design. Feature 06 extends `mock_trade_gateway.py`'s `MockTradeGateway` with
`submit_order`/`cancel_order`/`query_order_reports`/`query_fills` and scripted
`simulate_ack`/`simulate_reject`/`simulate_fill`/`simulate_cancel_confirmed`/
`replay_last_fill` methods, and adds the same four methods to
`broker_session_gateway_views.py`'s `BrokerSessionTradeGatewayView` as honest
`NotImplementedError` stubs — real vendor order-submission wiring is deferred, see
`docs/adr/0008-order-and-fill-state-machine.md`. Feature 07 adds one more
`MockTradeGateway` scripting method, `set_positions()`, so tests can simulate the
broker's position query changing over time independent of any simulated fill. Feature 08
adds `fail_next_query_positions()`, so tests can script a transient
`query_positions()` failure (raise once, then resume normal scripted results) without a
real vendor connection — see `docs/adr/0010-position-reconciliation-and-manual-sync.md`.
Feature 09 adds `mock_broker_session.py`'s `MockBrokerSession.script_start_failures()`
(the next N `start()` calls publish `BrokerLoginFailed` before falling back to the
scripted happy path) and `start_calls` (every `start()` call, in order), so
`ConnectivityMonitor`'s reconnect retry/exhaustion behavior can be tested without a real
vendor connection — see `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`.

## `persistence`

SQLite storage. Feature 01 only wired `sqlite_connection.create_connection()` (now with
an optional `check_same_thread` param). The two-month bar-history extension adds the
first real schema/repository: `sqlite_bar_record_repository.py`
(`SqliteBarRecordRepository`, implementing `application.ports.bar_record_repository.
BarRecordRepository` — `bar_records` + `bar_record_revisions` tables, prices/timestamps
stored as exact-round-trip `TEXT`, a `threading.Lock` serializing access from the
writer thread/UI thread/event thread). The yfinance-backfill revision of that same
extension adds a third table, `bar_backfill_conflicts` (append-only, same shape as
`bar_record_revisions` — both sides' full OHLCV summary plus `detected_at`), backing the
new `record_conflict`/`list_conflicted_trading_days` port methods. Broader
migrations/other repositories remain
Feature 14's job — see `docs/adr/0007-two-month-bar-history-persistence.md`. Feature 06
adds `sqlite_order_repository.py` (`SqliteOrderRepository`, implementing
`application.ports.order_repository.OrderRepository` — a single `order_intents` table,
one row per order intent, mutated via `UPDATE` rather than append-only like bar records).
Deliberately its **own** `sqlite3.Connection`/file (`orders.sqlite3`), never sharing
`market_data.sqlite3`'s connection — two independently-locked repositories over one
shared connection would not actually mutually exclude each other. See
`docs/adr/0008-order-and-fill-state-machine.md`. Feature 07 adds
`sqlite_reversal_workflow_repository.py` (`SqliteReversalWorkflowRepository`,
implementing `application.ports.reversal_workflow_repository.
ReversalWorkflowRepository` — same one-row-per-record/`UPDATE`/own-connection shape,
its own `reversal_workflows.sqlite3` file). See
`docs/adr/0009-safe-reversal-and-scaling.md`. Feature 08 adds
`sqlite_position_baseline_repository.py` (`SqlitePositionBaselineRepository`,
implementing `application.ports.position_baseline_repository.
PositionBaselineRepository` — one row per account/instrument/contract, its own
`position_baselines.sqlite3` file; a plain `INSERT ... ON CONFLICT DO UPDATE` upsert
rather than the insert/update split the other two repositories use, since a baseline has
no idempotency/trigger-key dedup concept). See `docs/adr/0010-position-reconciliation-
and-manual-sync.md`.

## `desktop`

The composition root (`composition.py`, `build_services()`/`load_settings()`) and the
wxPython UI shell. `app.py` (the `wx.App`), `readiness_frame.py` (the startup
diagnostics + session-control screen — no order-sending control anywhere; Feature 02
adds a Connect/Disconnect/account-picker for the broker session, which is
session-lifecycle control, not order submission), `instrument_selection_panel.py`
(Feature 03 — instrument/contract pick, AUTO/MANUAL mode, resolved-contract preview and
explicit confirm-and-switch button, embedded in `ReadinessFrame`; also
session-lifecycle-adjacent, not order submission), `market_data_panel.py` (Feature 04 —
forming-bar OHLCV, closed-bar list with red/black/doji marker, last-update time, and
stale/gap badges; a pure display surface, `ReadinessFrame` owns the event subscriptions
and calls its `refresh()`; the two-month bar-history extension adds a recorded-range
label and a red/black/doji streak display, and folds "recent" and "historical" closed
bars into one list backed by `MarketDataBarService.query_history()` — a date-range
picker + 查詢 button lets the operator jump to a different day, but every `refresh()`
re-queries the currently-selected range so today's view keeps updating live without a
separate "recent bars" data path; the yfinance-backfill revision of the same extension
makes each row's displayed 來源 honest per-record — `AGGREGATED_FROM_YUANTA_REALTIME` vs.
`BACKFILLED_FROM_YFINANCE` — instead of a single hardcoded label), `connectivity_panel.py`
(Feature 09 — per-channel
connectivity health table, the current `SafePauseRecord`'s reason/detected/effective time
if any, reconnect-attempt status, and the one operator control this feature adds, a "停止
重連" button bound to `ConnectivityMonitor.cancel_reconnect()`; same pure-display,
`ReadinessFrame`-owns-the-subscriptions pattern as `MarketDataPanel` — no "press
continue"/resume control exists, since nothing yet drives `StrategyState` into
`STARTING`/`RUNNING` at all, same documented gap as Feature 06/07/08), `__main__.py` (the
`python -m tfx_quant.desktop` entrypoint — also starts/stops `MarketDataBarService`'s,
`BarHistoryBackfillService`'s (the two-month bar-history extension's yfinance-backfill
revision), (Feature 06) `OrderManager`'s, (Feature 08) `PositionReconciliationService`'s,
and (Feature 09) `ConnectivityMonitor`'s background timers alongside the
`EventCoordinator`'s; `ReversalWorkflowService`/`ScalingService` (Feature 07) need no
start/stop of their own — neither owns a background timer; its uncaught-exception
handler now calls the shared `domain.strategy_state.attempt_safe_pause()` helper rather
than inlining the PAUSED_SAFE-else-FAULTED fallback logic itself). `app.py`'s
`TfxQuantApp` (Feature 08) binds `wx.EVT_ACTIVATE_APP` to call
`PositionReconciliationService.on_foreground_return()` whenever the window becomes
active — the "回到前景時查詢持倉" trigger. `composition.py` also resolves
`TradingSettings.market_data_db_path` (defaulting to a per-user
`%LOCALAPPDATA%/tfx_quant/market_data.sqlite3`) and wires `SqliteBarRecordRepository`
into `MarketDataBarService`, resolves `TradingSettings.yahoo_ticker_mapping_path`
(defaulting to the bundled, deliberately-empty `yahoo_ticker_mapping.example.json`) into
`JsonYahooTickerMappingRepository`, and wires `use_mock`'s `MockYahooHistoryQuery` or
(lazily imported, same isolation rationale as the SPARK API adapter)
`YfinanceHistoryQueryAdapter` plus both of the above into `BarHistoryBackfillService`
(the two-month bar-history extension's yfinance-backfill revision — see
`docs/adr/0007-two-month-bar-history-persistence.md`'s 2026-08-21 extension), whose
`is_degraded()` backs a new `compute_readiness()` row ("Market data: yfinance
backfill"), (Feature 06) resolves `TradingSettings.order_db_path`
(defaulting to `%LOCALAPPDATA%/tfx_quant/orders.sqlite3`) and wires
`SqliteOrderRepository` into `OrderManager`, (Feature 07) resolves
`TradingSettings.reversal_workflow_db_path` (defaulting to
`%LOCALAPPDATA%/tfx_quant/reversal_workflows.sqlite3`) and wires
`SqliteReversalWorkflowRepository` into `ReversalWorkflowService`, and (Feature 08)
resolves `TradingSettings.position_baseline_db_path` (defaulting to
`%LOCALAPPDATA%/tfx_quant/position_baselines.sqlite3`) and wires
`SqlitePositionBaselineRepository`/`PositionReconciliationService` — whose
`expected_net_lookup` now replaces Feature 06's always-flat `position_lookup`
placeholder as `OrderManager`'s real position source. (Feature 09) builds
`ConnectivityMonitor` against the *raw* `broker_session` before rebinding both
`trade_gateway`/`broker_session` to their `application.connectivity.gateway_tracking`
tracking wrappers for every other service — avoiding a construction cycle, since the
wrappers themselves need a `ConnectivityMonitor` reference — and calls
`connectivity_monitor.attach_reconnect_reconciliation_watcher()` last, strictly after
`bar_history_backfill_service`/`reconciliation_service`/`order_manager` have all
subscribed their own `BrokerSessionReady` handlers (see `docs/adr/0011-connectivity-
reconnect-and-safe-pause.md`'s wiring-order decisions). `compute_readiness()` gains one
Feature 09 row ("Connectivity: no unresolved safe-pause"). No order-entry UI control
exists yet, so nothing in this package calls `OrderManager.submit()`/
`ReversalWorkflowService.start_reversal()`/`ScalingService.evaluate_and_submit()`, and
the manual-sync confirmation button (`PositionReconciliationService.
confirm_manual_sync()`) has no UI caller yet either (see `docs/adr/0008-order-and-fill-
state-machine.md`, `docs/adr/0009-safe-reversal-and-scaling.md`, `docs/adr/0010-position-
reconciliation-and-manual-sync.md`, and `docs/adr/0011-connectivity-reconnect-and-safe-
pause.md`). The only package allowed to depend on everything else; nothing depends on it.

## `tests`

Mirrors the `src/tfx_quant/` package structure (`tests/domain/`,
`tests/application/`, `tests/infrastructure/`, `tests/persistence/`, `tests/desktop/`).
