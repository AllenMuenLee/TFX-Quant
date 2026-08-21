# ADR 0010 — Position reconciliation and manual sync (Feature 08)

## Status

Accepted.

## Context

`implementation prompt/08-position-reconciliation/implementation-prompt.md` asks for a
continuous, independent comparison of three things — the broker's actual position, this
system's own fill-derived "expected position", and (implicitly) the strategy's own
belief — at every point that matters (login, strategy start, every fill, the reversal
full-flat gate, reconnect, a timed poll, and returning to the foreground), with an
immediate safe pause on any direction/quantity mismatch, and a two-step, fully-audited
manual sync as the only way to adopt the broker's number as the new baseline.

Feature 06 (`docs/adr/0008-order-and-fill-state-machine.md`, decision 10) left
`OrderManager`'s `position_lookup` as an always-flat placeholder, explicitly deferred:
"Feature 06 has no real position tracker yet (that's Feature 08's job)." This feature
closes that gap. It was built after Feature 07 (safe reversal/scaling), per the
implementation-prompt README's recommended order, and — like Feature 07 before it — has
no strategy engine (Feature 05 still doesn't exist as an application service) or
order-entry UI (Feature 12) calling into it automatically yet for the two triggers that
depend on those ("啟動策略", displaying the manual-sync confirmation button).

## Decisions

### 1. "Expected position" is a persisted, incrementally-updated baseline — never
re-derived by replaying order history at query time

`domain/position_reconciliation.py`'s `PositionBaseline` (one row per account/
instrument/contract, `persistence/sqlite_position_baseline_repository.py`) starts
assumed-flat the first time a contract is seen, moves by exactly the signed fill
quantity on every `FillReceived` this system's own `OrderManager` observes
(`PositionReconciliationService._on_fill`), and is only ever replaced outright by an
explicit, human-confirmed `confirm_manual_sync()` call. A mismatch is never used to
silently "fix" the baseline — this is the literal implementation of "不得把持倉差異自動
解釋成策略成交，也不得自動下單「修正」差異." Crash recovery relies on the same trust
model `OrderIntent.filled_quantity` already uses (a synchronously-persisted running
total, not re-derived from a replay) — honest, and safe in practice because the
mandatory query-at-`LOGIN`/`RECONNECT` trigger independently re-verifies the baseline
against a fresh broker query immediately after every restart, before any automatic
order could ever be sent.

### 2. `PositionReconciliationService` drives `StrategyStateMachine` itself — the one
"fires reliably, doesn't enforce" exception

Every other cross-cutting safety event in this codebase (`BrokerSessionInvalidated`,
`MarketDataGapDetected`, `OrderRequiresManualReview`, `ReversalPausedSafe`) is
documented as "fires reliably, does not itself enforce a strategy-wide pause — gating
`StrategyState` is Feature 09/10's job." Position reconciliation is different: the
implementation prompt says "立即進入 `PausedSafe`" is *this* feature's job, not
deferred. `PositionReconciliationService._run()` calls the new
`domain.strategy_state.attempt_safe_pause()` helper (factored out of
`desktop.__main__`'s uncaught-exception handler, which now shares it) directly on every
detected discrepancy, and only publishes `PositionDiscrepancyDetected` afterward, for
observers (eventually the UI) to react to.

**A real bug this caught during testing**: the naive version called
`attempt_safe_pause` unconditionally on every discrepancy, including the *same still-
unresolved* mismatch re-detected by a later trigger (a second timed-poll tick, or the
manual-sync flow's own "重新查詢" step, both of which re-run the full comparison).
Since `PAUSED_SAFE -> FAULTED` is itself a legal transition, this silently escalated an
already-safely-paused strategy to `FAULTED` the moment the operator so much as clicked
"重新查詢" — exactly the kind of thing a human doing recovery work must never trigger by
accident. Fixed by only attempting a transition when the strategy is currently
`RUNNING`; any other state (including `PAUSED_SAFE`/`FAULTED` already) is reported as
itself in `ReconciliationRecord.resulting_strategy_state`, never escalated further.

### 3. A query failure is never treated as a discrepancy

`TradeGatewayPort.query_positions()` can fail transiently (network blip, broker-side
timeout — see the acceptance criteria's "查詢暫時失敗" scenario). `_run()` catches it,
logs `position_query_failed` with full context, and returns a `ReconciliationRecord`
with `actual_net=None`/`query_error` set and `paused=False` — distinct from a genuine
mismatch, which always has `actual_net` populated. Nothing is silently assumed either
way; the caller is expected to check `ReconciliationRecord.query_succeeded` before
trusting `discrepancy`. A real mismatch already sitting behind a flaky connection is
still guaranteed to be caught on "下一次回報或輪詢" per the acceptance text — the next
poll, fill, or reconnect will simply query again.

### 4. Only the current account's exact contract is compared; every other position is
a logged warning, never part of the gate

"僅比較目前交易帳號與完整契約；同時顯示其他契約持倉供警示" is implemented literally:
`_run()` filters the broker's full position list down to the one (account, instrument,
contract) match for the comparison, and separately logs every other same-account
position (`reconciliation_other_contract_positions_detected`, `WARNING`) plus a compact
`other_contract_position_count` on the record — informational only, never blocking or
pausing anything by itself. There is no UI to actually display this warning yet
(Feature 12's job); the structured log and the count are the honest, present-day
surface.

### 5. The two-step manual sync: re-query is just another `reconcile()` call; confirm
re-verifies fresh, never trusts the caller's number alone

"重新查詢" (`request_manual_requery()`) is `reconcile(trigger=MANUAL_REQUERY)` — no
separate code path, so it gets exactly the same structured logging and other-contract
warning as every other trigger, and its returned `ReconciliationRecord` (account,
instrument, contract, `actual_net`, `broker_snapshot_at`) is everything the "顯示帳號、
商品、契約、實際口數與時間" confirmation button needs. "確認以元大實際持倉同步"
(`confirm_manual_sync()`) never trusts the operator-supplied `confirmed_actual_net`
alone — it re-queries the broker fresh and raises `StaleSyncConfirmationError` if the
position has moved again since the display the operator is confirming against, forcing
a fresh requery rather than silently accepting stale data. The "同步前必須確認無活動或
未知委託；有未知委託時禁止同步" gate is `domain.position_reconciliation.
ManualSyncPreflight` — same itemized-boolean shape as Feature 07's
`FlatConfirmationResult` — checked against the *local* `OrderRepository` (the same
authoritative "active including `UNKNOWN`" source `OrderManager`/
`ReversalWorkflowService` already use), not a fresh broker order query.

### 6. A confirmed sync resets K/signal state via the existing seam, and retires (never
resumes) any paused reversal workflow — a narrowly-scoped, one-line exception to
"`PAUSED_SAFE` is terminal"

"重置連續 K、最後訊號" reuses Feature 03/04's existing `BarSignalStateStore.clear()`
seam — the same one `InstrumentSelectionService.switch_to()` already calls, no new
mechanism needed. "重置...反手／加碼 workflow" has nothing to do for `ScalingService`
(Feature 07 ADR 0009 decision 1: it has no persisted state at all), but a `PAUSED_SAFE`
`ReversalWorkflowRecord` for the synced contract would otherwise block a fresh reversal
forever — `ReversalWorkflowStateMachine` had no legal transition out of `PAUSED_SAFE` at
all (deliberately, per ADR 0009 decision 3: "nothing auto-resumes a paused workflow").
`domain/reversal_workflow.py` now allows exactly one additional edge,
`PAUSED_SAFE -> BLOCKED`, exercised only by
`PositionReconciliationService._reset_paused_reversal_workflows()` as part of a
human-confirmed sync — never automatically, and it is a *retirement* (`BLOCKED` already
means "will never proceed, slot freed"), not a resume: the workflow's close/entry legs
never advance again, only the "one workflow per contract" slot frees up so a *fresh*
reversal can start once the operator restarts the strategy.

### 7. The sync itself never sends an order, and never resumes `RUNNING`

`confirm_manual_sync()` calls `TradeGatewayPort.query_positions()` (read-only) and
`OrderRepository`/`ReversalWorkflowRepository`/`PositionBaselineRepository` writes only
— no code path here ever touches `OrderManager.submit()` or
`TradeGatewayPort.submit_order()`, satisfying "同步本身不送任何單" structurally, not by
convention. Nor does it transition `StrategyStateMachine` at all — whatever state the
machine was already in (typically `PAUSED_SAFE`, from the mismatch that triggered the
recovery) is simply reported back as `ManualSyncRecord.still_paused_safe`, requiring the
operator to explicitly restart per "保持暫停，必須再次人工啟動."

### 8. Own dedicated SQLite connection/file for baselines

`SqlitePositionBaselineRepository` never shares `orders.sqlite3`'s,
`reversal_workflows.sqlite3`'s, or `market_data.sqlite3`'s connection — same
lock-hazard reasoning as ADR 0008 decision 7 and ADR 0009 decision 7. Unlike
`OrderRepository`/`ReversalWorkflowRepository` there is no idempotency/trigger-key dedup
concept for a baseline (naturally one row per contract), so it's a plain
`INSERT ... ON CONFLICT DO UPDATE` upsert rather than the insert/update split those two
repositories use.

## Consequences

- `OrderManager`'s `position_lookup` (Feature 06 ADR 0008 decision 10's documented gap)
  is now `PositionReconciliationService.expected_net_lookup` — `desktop/composition.py`
  had to move `SqliteReversalWorkflowRepository`'s construction earlier (it's now also a
  `PositionReconciliationService` dependency, for the reversal-flat-gate trigger and the
  manual-sync reset) so `OrderManager` can be built with the real lookup instead of the
  removed `_flat_position_lookup` placeholder.
- Two triggers remain documented wiring points with no automatic caller yet, same
  "ports-and-mock-now, full-feature-later" posture as every prior feature:
  `on_strategy_start()` (no code path transitions `StrategyState` into `RUNNING`
  automatically — Feature 05/12's job) and the manual-sync confirmation button itself
  (no UI exists — Feature 12's job). `on_foreground_return()` *is* wired, from
  `desktop/app.py`'s `wx.EVT_ACTIVATE_APP` handler, since that trigger needed no new UI
  surface, just an existing wx event.
- `MockTradeGateway.query_positions()` gained `fail_next_query_positions()` scripting
  (raise once, then resume normal scripted results) to test the "查詢暫時失敗"
  acceptance scenario without a real vendor connection.
- **"阻止所有自動新單" is enforced structurally only as far as `StrategyState` itself
  goes** — `OrderManager.submit()` has never checked `StrategyState` (not in Feature 06,
  not in Feature 07, not here); nothing in this codebase automatically calls `submit()`
  at all yet, since Feature 05 (the strategy engine) doesn't exist as an application
  service. `PAUSED_SAFE` is the real, correctly-driven signal a future strategy engine
  must check before calling `OrderManager.submit()`/`ReversalWorkflowService.
  start_reversal()`/`ScalingService.evaluate_and_submit()` — this is the same
  "ports-and-mock-now, full-feature-later" gap every prior feature has left honestly
  documented rather than papered over with a check that has no real caller to protect
  yet.
