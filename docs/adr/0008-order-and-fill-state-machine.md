# ADR 0008 — Order and fill state machine (Feature 06)

## Status

Accepted.

## Context

`implementation prompt/06-order-and-fill-state-machine/implementation-prompt.md` asks for
the single component allowed to turn a trading intent into a real Yuanta order:
idempotent, tolerant of duplicate/out-of-order broker callbacks, never treats "the API
call returned" as "the order happened," blocks a second workflow on the same
account/contract, enforces the 2-lot worst-case-exposure cap, and lands in a safe,
human-reviewable state on any ambiguity (timeout, partial fill, reject, unknown order,
missing fields) — never auto-resending.

Before this feature there was no order-sending code path anywhere in this codebase at
all — `application/ports/yuanta_gateways.py`'s own docstring said so explicitly, and
`domain/order.py` only had the request shape (`Order`, `ClientOrderId`), no status, no
broker order number, no fill accumulation. `application/events/events.py` already
declared `OrderReportReceived`/`FillReceived` but nothing published or subscribed to
them, so both were free to reshape.

## Decisions

### 1. Three separate IDs, not one

`domain/order_state_machine.py` distinguishes `LocalOrderId` (this process's own primary
key), `ClientOrderId` (the broker-facing idempotency/correlation tag — already existed
from Feature 01), and a caller-supplied `idempotency_key` string on `OrderRequest`. A
caller (a future strategy engine, a manual UI action) only ever sees `OrderRequest` —
never builds `Order`/`ClientOrderId` itself. `OrderManager.submit()` mints both only once,
the first time it sees a given `idempotency_key`; a later `submit()` call with the same
key returns the existing `OrderIntent` unchanged and never calls the gateway again. This
is deliberately *not* collapsed into "the caller reuses its own `ClientOrderId` on retry"
— that would let a careless caller defeat the dedup guarantee by minting a fresh
`ClientOrderId` on every retry. `workflow_id` is a fourth, purely-descriptive field
(unused for dedup) reserved for a future feature (Feature 07's reversal, which spans two
orders) to group related intents.

### 2. `OrderStatus` transition table, plus a special "UNKNOWN blocks control flow, not
fact-recording" rule

At least the nine states the prompt lists (`CREATED, SUBMITTING, ACKNOWLEDGED,
PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, UNKNOWN`), with a
transition table in `domain/order_state_machine.py` mirroring `domain/strategy_state.py`'s
shape. The one non-obvious rule: `UNKNOWN` is excluded from `_TERMINAL_STATUSES` — every
other terminal state (`FILLED`/`CANCELLED`/`REJECTED`) accepts no further transition at
all, but `UNKNOWN` can still move to `ACKNOWLEDGED`/`PARTIALLY_FILLED`/`FILLED`/
`CANCELLED`/`REJECTED`. This is what makes the "逾時後晚到成交" acceptance scenario work:
a timeout pushes an order to `UNKNOWN` (blocking all further automatic control flow — no
resend, and it still occupies the "one workflow per contract" slot), but a late,
authoritative broker report or fill must still be recordable, or the local ledger would
permanently disagree with the broker's own books. There is no resend code path anywhere
in this codebase — a fresh order is always a brand new `OrderManager.submit()` call with
a fresh `idempotency_key`, which is always a deliberate, separate action.

### 3. One shared per-order sequence number, not two

Both `OrderReport.broker_seq_no` and `Fill.broker_seq_no` feed the same dedup/ordering
gate on `OrderIntent.last_applied_broker_seq_no` — an incoming report or fill whose
`broker_seq_no` does not exceed the last one actually applied is a duplicate or
out-of-order replay, logged and ignored (`OrderStateMachine.apply_order_report`/
`apply_fill` both return `(record, applied=False)` rather than raising). This assumes the
broker (or, until a real vendor adapter exists, the mock) assigns one monotonic sequence
per order across both report kinds — unverified against the real SPARK API, flagged the
same way other vendor-behavior assumptions are flagged elsewhere in this codebase (e.g.
ADR 0007's `GetKLine` timestamp-labeling assumption).

### 4. Synchronous persist-then-call, not an async write queue

`MarketDataBarService`'s bar-write path (ADR 0007 decision 5) is deliberately async — a
bounded queue plus a dedicated writer thread — because it sits behind a per-tick hot path
where a slow database must never block market-data processing. `OrderManager.submit()`
does the opposite on purpose: `OrderRepository.save_intent()` is called synchronously,
inline, and **must complete** before `TradeGatewayPort.submit_order()` is ever called —
this is the literal "送單前原子地持久化 intent...再呼叫 API" requirement. Order volume is
low (this is not a per-tick path), and the entire point of this component is a *stronger*
durability guarantee than the bar-write path offers, not higher throughput. Every later
transition (`update_intent`) is likewise synchronous, on whichever thread the triggering
event/call arrived on.

### 5. Worst-case exposure: a general fold over active orders, not an assumed single-order
shortcut

`domain.order_state_machine.worst_case_net_position_range(current, active)` folds every
still-active order's *remaining* quantity (assumed independently able to fully fill, in
its own direction) onto the current position, returning a `[min, max]` reachable range —
the literal "使用送單前持倉加上所有可能成交的活動委託計算最壞曝險" wording.
`OrderManager`'s own "one workflow per account/contract" rule (`find_active_for_contract`
raising `ActiveWorkflowInProgressError` when non-empty) normally keeps `active` to at most
one entry by the time `submit()`'s exposure check runs, but the exposure function itself
doesn't assume that invariant — it's written generally, defensively, not coupled to a
call-site guarantee that could erode later.

### 6. `OrderIntentSaveOutcome.DUPLICATE_KEY`, not an exception, for the ordinary
idempotency case — same split as `BarUpsertOutcome`

`OrderRepository.save_intent()` returns an outcome enum (`INSERTED`/`DUPLICATE_KEY`)
rather than raising for the "already exists" case, backed by a `UNIQUE(idempotency_key)`
SQLite constraint that `SqliteOrderRepository` catches (`sqlite3.IntegrityError`) and
translates — the same "ordinary outcome vs. genuine I/O failure" split
`BarUpsertOutcome`/`BarUpsertRepositoryError` established for bar records. This gives an
atomic, DB-level idempotency guarantee (protects against a hypothetical race between two
near-simultaneous `submit()` calls) on top of `OrderManager`'s own cheaper in-memory
pre-check (`find_by_idempotency_key`).

### 7. Order intents get their own SQLite connection/file — never share
`market_data.sqlite3`'s connection

`SqliteBarRecordRepository` and the new `SqliteOrderRepository` each hold their own
private `threading.Lock` serializing access to *their* `sqlite3.Connection`. If both
repositories were constructed over the *same* connection object, those two independent
locks would not mutually exclude each other — a real concurrency hazard, since the bar
writer thread and `OrderManager`'s event-handler writes (on the `EventCoordinator`
dispatch thread, the UI thread, or a timer thread) can all be active at once.
`desktop/composition.py` opens a second `create_connection(...)` against a dedicated
`orders.sqlite3` file (new `TradingSettings.order_db_path`, defaulting to
`%LOCALAPPDATA%/tfx_quant/orders.sqlite3`, mirroring `market_data_db_path`'s existing
resolution pattern) rather than reusing `market_data_connection`.

### 8. One row per order intent, mutated via `UPDATE` — not one row per event

Unlike bar records (one row per closed bar, `INSERT`-only after the first write, revised
only through an explicit, audited `apply_correction`), `order_intents` has exactly one row
per `local_order_id`; `save_intent` inserts it once, every later transition is an
`UPDATE` against that same row. A separate fills-audit table was considered (mirroring
`bar_record_revisions`) but dropped: the domain-level `last_applied_broker_seq_no` gate
already fully satisfies "重複成交...不得建立重複資料," and the "由 fill ID 串回原 intent"
requirement is a *logging* requirement — every fill-related log record already carries
both `broker_fill_no` and `local_order_id`/`client_order_id` together — not a persistence
one. Adding a table nothing reads would be unjustified complexity.

### 9. Real SPARK API order-submission wiring is out of scope for this pass

The prompt's own acceptance criteria only requires a mock-adapter test suite ("用模擬
adapter 測試"), and this codebase's established pattern (Feature 01→02; Feature 03/04's
own still-deferred `query_open_orders`/`query_positions` parsing) is ports-and-mock
first, real vendor adapter only once someone with real credentials has read the live
委託/回報 SPARK API docs — fabricating vendor method names now would violate the
project's "不得臆測：API 名稱、參數..." rule. `BrokerSessionTradeGatewayView` gets
`submit_order`/`cancel_order`/`query_order_reports`/`query_fills` as honest
`NotImplementedError` stubs, same style as its pre-existing `query_open_orders`/
`query_positions`. `OrderManager` and its full test suite are built entirely against the
extended `MockTradeGateway` instead.

### 10. `position_lookup` is an injected callable, not a new port

`OrderManager`'s exposure check needs the account's current net position, but Feature 06
doesn't own position tracking — that's Feature 08's job. Rather than invent a
`PositionRepository` port ahead of that feature actually needing one,
`OrderManager.__init__` takes a small `position_lookup: Callable[[TradingAccount,
Instrument, ContractMonth], NetPosition]`. `desktop/composition.py` wires in a one-line,
explicitly-documented placeholder (`_flat_position_lookup`, always `NetPosition(0)`) —
kept honest by its docstring rather than hidden behind an abstraction that implies more
than it currently delivers. Replace this wiring, not the `OrderManager` constructor
signature, once Feature 08 lands.

## Consequences

- `OrderManager` is wired into `desktop/composition.py`/`__main__.py`'s start/stop
  lifecycle, but nothing in the desktop shell calls `OrderManager.submit()` yet — no
  order-entry UI exists (out of this feature's acceptance criteria) and no strategy
  engine drives it automatically. `reconcile_on_startup()` does run automatically on
  every `BrokerSessionReady` (first connect and every reconnect).
- The exposure check's real-world usefulness is currently limited by decision 10's flat
  placeholder — until Feature 08 wires a real position lookup, `OrderManager` only ever
  sees "flat plus this order's own active-workflow bookkeeping," not the broker's actual
  current position. This is an honest, documented gap, not a silent one.
- Real vendor order submission is unverified end-to-end, same honest status the rest of
  the SPARK API rewrite carries (see `[[yuanta-spark-api-pivot]]`) — a future session with
  real credentials must read the live 委託/回報 docs before filling in
  `BrokerSessionTradeGatewayView`'s four stub methods.
