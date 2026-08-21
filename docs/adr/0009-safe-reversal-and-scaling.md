# ADR 0009 — Safe reversal and scaling (Feature 07)

## Status

Accepted.

## Context

`implementation prompt/07-safe-reversal-and-scaling/implementation-prompt.md` asks for
reversal ("反手") and add-on ("加碼") to be modeled as recoverable, persisted, multi-step
application workflows sitting above `application.order_management.OrderManager`
(Feature 06, "唯一有權將交易意圖轉成元大委託的 order manager") — never sending a single
doubled-quantity order to simulate a reversal, and never submitting a reverse entry order
until the original position's full-close fill has been independently corroborated by a
fresh broker position query showing exactly 0.

Feature 05 (strategy signal engine) does not exist as an application service yet — only
`domain/signal.py`'s `StrategySignal`/`SignalKind` predate this, unused anywhere in `src/`
per grep. This feature was built ahead of Feature 05, per the implementation-prompt
README's own recommended order, and defines its own clean entry points
(`ReversalWorkflowService.start_reversal`/`ScalingService.evaluate_and_submit`, each
taking an explicit `account`/`price`/`trigger_key`/`idempotency_key`) that Feature 05
will call once it exists — the same way `OrderManager.submit()`'s `OrderRequest` was
defined ahead of any real caller in Feature 06.

## Decisions

### 1. Reversal is a persisted state machine; scaling is not

`domain/reversal_workflow.py`'s `ReversalWorkflowState` (`STARTED, POSITION_QUERIED,
CLOSE_ORDER_SUBMITTED, CLOSE_FILLED_BY_REPORT, FLAT_CONFIRMED, ENTRY_ORDER_SUBMITTED,
COMPLETED, PAUSED_SAFE, BLOCKED`) models reversal's genuinely multi-step, crash-spanning
nature — two sequential orders with an independent verification gate in between, which
can be interrupted at any point and must resume correctly. Scaling is a single order
behind a synchronous gate check (`application/reversal_scaling/gates.
evaluate_scaling_gate`); `ScalingService` has no persisted workflow of its own because it
needs none — `OrderManager`'s existing idempotency-key dedup and its existing partial-
fill/reject/timeout/unknown → `OrderRequiresManualReview` handling (both built in Feature
06) already fully satisfy "不得因後續 K 棒重送" and "加碼部分成交或狀態不明亦須暫停" with
zero additional code.

### 2. Reversal-side arithmetic is shared between both legs, computed once

Closing a short position means buying; reversing a short into long also means buying one
more. Closing a long means selling; reversing into short also means selling one more. So
`reversal_side_for(starting_net)` (pure, in `domain/reversal_workflow.py`) returns one
`Side` used for *both* the close leg (quantity `abs(starting_net.lots)`, `OrderKind.
CLOSE`) and the entry leg (quantity always exactly `1`, `OrderKind.OPEN`) — computed once,
at `POSITION_QUERIED`, and persisted on the record rather than re-derived at each step
(so a crash between steps can't have it drift). A starting position of exactly `0`
returns `None` and blocks the workflow (`已無持倉，無需反手`) rather than guessing a fresh
entry — that ambiguity belongs to the signal engine (`ENTER_LONG`/`ENTER_SHORT`), not this
workflow.

### 3. `PAUSED_SAFE` is workflow-terminal; nothing auto-resumes a paused workflow

Same philosophy as `domain.order_state_machine.OrderStatus.UNKNOWN` in Feature 06: no
resend/auto-resume code path exists anywhere in this codebase. `ReversalWorkflowService.
resume_pending_workflows()` explicitly skips every `PAUSED_SAFE` record
(`_reload_tracked()` and the resume sweep both exclude it), and once a workflow pauses,
its linked `ClientOrderId`s are removed from the in-memory tracking map
(`_untrack`) — so a late-arriving `OrderStateTransitioned`/`OrderRequiresManualReview`
event for either leg is still fully processed and recorded at the *order* level by
`OrderManager` (per Feature 06), but no longer drives this *workflow* forward. This is
the concrete mechanism behind "晚到成交" not silently resurrecting a paused reversal, and
the reason `PAUSED_SAFE` still counts as `is_active` for the "one workflow per contract"
guard — an unresolved reversal keeps blocking a new one from starting rather than
silently freeing the slot.

### 4. Crash-safety is not re-implemented — it's inherited from `OrderManager`'s idempotency keys

Every order submission uses a deterministic, workflow-derived idempotency key
(`f"{workflow_id}:close"` / `f"{workflow_id}:entry"`). This means every step function in
`ReversalWorkflowService._advance*` is safe to call again after a crash: `submit()` for
an already-submitted leg simply returns the existing `OrderIntent` without a second
broker call (Feature 06's own guarantee), so `resume_pending_workflows()` never needs its
own separate "did I already send this" bookkeeping — it re-derives forward progress
purely from fresh queries (`OrderRepository.find_by_client_order_id` for whichever leg is
outstanding) and safely re-enters the same step-dispatch logic (`_advance`) that the live
event-driven path uses. Resuming from `FLAT_CONFIRMED` twice in a row (double-resume)
resubmits nothing the second time — the entry order's `OrderRepository` status is by then
`SUBMITTING`/`ACKNOWLEDGED` and is left alone, "still in flight."

### 5. The flat-confirmation gate is four independently-logged booleans, and any single
failure pauses — no partial-retry loop

`domain.reversal_workflow.FlatConfirmationResult` (`is_flat`, `position_lots`,
`has_active_or_unknown_orders`, `session_healthy`, `market_data_healthy`) is the
"gate 的逐項結果" debug-log requirement made structural rather than a single collapsed
boolean — every sub-check is independently inspectable in logs and in the
`ReverseEntryBlocked` event's `result` field. A failure for *any* reason (including the
explicit "全成但持倉仍非零" contradiction between the fill report and a fresh position
query) pauses immediately; this codebase's established "any ambiguity → safe pause, no
auto-retry" philosophy (see Feature 06) was chosen over a bespoke poll/retry subsystem
that nothing in the prompt actually asked for.

### 6. The EOD margin is a narrow, local safety check — not Feature 10's job, done here

`gates.is_too_close_to_eod(now, margin)` is a coarse, date-agnostic time-of-day
comparison against `TradingSettings.REQUIRED_EOD_FLATTEN_TIME` (04:55) minus a
configurable margin (default 10 minutes) — never trading-day/session-boundary aware
(unlike `domain.trading_calendar`), since it only needs to refuse *starting* a new
multi-step reversal/scaling workflow shortly before the mandatory flatten deadline (it
can't safely finish in time), not implement the deadline itself. The actual 04:55
end-of-session flatten workflow, the 08:45/09:45/10:45 entry-time-window enforcement, and
the system-wide risk supervisor are explicitly `implementation prompt/10-risk-eod-and-
emergency-flatten/implementation-prompt.md`'s job — this mirrors Feature 06 ADR 0008
decision 9's "ports-and-mock-now, full-feature-later" scoping discipline. `start_reversal`
called inside the margin returns a `BLOCKED` record and publishes `ReverseEntryBlocked`
with `result=None` (no position/order query ever happens — there's nothing to report on
yet).

### 7. Own dedicated SQLite connection/file for reversal workflows

`SqliteReversalWorkflowRepository` never shares `orders.sqlite3`'s or
`market_data.sqlite3`'s connection — same lock-hazard reasoning as ADR 0008 decision 7:
two independently-locked repositories over one shared `sqlite3.Connection` would not
actually mutually exclude each other. `TradingSettings.reversal_workflow_db_path`
(defaulting to `%LOCALAPPDATA%/tfx_quant/reversal_workflows.sqlite3`) mirrors
`order_db_path`'s resolution pattern exactly.

## Consequences

- `ReversalWorkflowService`/`ScalingService` are wired into `desktop/composition.py`'s
  `ServiceContainer` and `ReversalWorkflowService` auto-resumes pending workflows on every
  `BrokerSessionReady` (first connect and every reconnect), but nothing in the desktop
  shell calls `start_reversal`/`evaluate_and_submit` yet — no order-entry UI exists (out
  of this feature's acceptance criteria) and no strategy engine drives either service
  automatically. Neither service owns a background timer, so `desktop/__main__.py` needs
  no new start/stop calls (unlike `OrderManager`'s timeout sweep).
- The exposure check both services indirectly rely on (via `OrderManager.submit()`'s own
  worst-case-exposure validation) is still limited by Feature 06 ADR 0008 decision 10's
  flat `position_lookup` placeholder — this is an existing, already-documented gap, not a
  new one introduced here.
- No real Yuanta vendor-adapter work was needed for this feature at all — everything goes
  through `OrderManager`/`TradeGatewayPort`, both already fully specified by Feature 06.
