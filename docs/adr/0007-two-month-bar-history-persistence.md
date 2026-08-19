# ADR 0007 — Two-month bar-history persistence (Feature 04 extension)

## Status

Accepted.

## Context

The separate implementation prompt `implementation prompt/04-market-data-and-60m-bars/
two-month-bar-history-implementation-prompt.md` requires persisting every closed,
validated 60-minute bar this software has itself aggregated, so an operator can review
the past two rolling calendar months of self-recorded history rather than only the
in-memory `recent_closed` deque (`MarketDataBarService`, capped at 20 bars and lost on
restart). ADR 0006 built the aggregation pipeline but left persistence entirely out of
scope — `persistence/sqlite_connection.py` was a bare connection factory with no schema.
This ADR covers what was added on top without re-litigating ADR 0006's aggregation
decisions (bar-label semantics, cut-point derivation, tick dedup) — see [[market-data-
and-bar-aggregation]] for those.

The prompt is explicit that no vendor historical-bar API exists or may be invented (same
posture ADR 0006 decision 7 already took for the undocumented Tick subsystem) — so this
is a "record what this process actually observed" pipeline, not a backfill.

## Decisions

### 1. `BarRecord` wraps `Bar` rather than extending it

The prompt requires storing 週期/交易日/session/來源/完整性/建立與更新時間 alongside
每筆 K棒. Adding all of that directly onto `domain.bar.Bar` would force every existing
call site (aggregator, streak counter, UI, tests across Features 03-04) to know about
persistence metadata that's irrelevant to them. `domain/bar_record.py`'s `BarRecord`
(`bar: Bar`, plus `period`/`trading_day`/`session`/`source`/`is_gap_recovery`/
`created_at`/`updated_at`/`revision`) is a separate value object used only by the
persistence path. `trading_day`/`session` are resolved via a new
`TradingCalendar.session_context_for()` method (mirrors `boundary_containing`'s
trading-day search, matched by exact boundary equality since every `Bar.start` this
codebase produces is already one of `bar_boundaries()`'s own outputs) — never read off
`bar.start.date()` directly, which would mis-attribute the night session's
post-midnight bars to the wrong trading day.

### 2. `BarPeriod`/`BarDataSource` exist as explicit enums with exactly one value today

`BarPeriod.SIXTY_MINUTE` and `BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME` are
single-member enums. This looks like premature generality but isn't: the prompt lists
週期 as part of the unique bar identity by name, and 來源 must never be presented as an
official Yuanta historical product (`AGGREGATED_FROM_YUANTA_REALTIME`'s name says this
directly) — both are controlled-vocabulary fields the prompt requires storing per row,
not derived data this codebase is guessing at.

### 3. Rolling two-month window: calendar months back, day-of-month clamped

`domain.bar_record.rolling_two_month_start(today)` subtracts 2 from the month index and
clamps the day-of-month to the target month's actual length (`calendar.monthrange`) —
never a fixed 60-day window, matching the prompt's own worked example (8/18 -> 6/18) and
its explicit ban on approximating with a fixed day count. Pure function, no I/O,
exhaustively tested for month-end clamping, leap-year clamping, and year-boundary
crossing (`tests/domain/test_bar_record.py`).

### 4. Continuity is adjacency between persisted bars, not a calendar re-derivation

`domain.bar_record.continuous_segments()` splits a sorted, deduped `BarRecord` sequence
into maximal runs where each bar's `start` exactly equals the previous bar's `end`. This
needs no `TradingCalendar`/`InstrumentMasterEntry` at all: since every persisted bar's
boundaries were themselves calendar-derived at aggregation time, any missing boundary —
a genuine data gap, a session change, or the software simply not running — breaks
adjacency automatically. `MarketDataBarService.continuous_warm_up_bars()` returns only
the tail segment (the one reaching the most recently persisted bar), which is the
"通過交易日曆與連續性檢查的區段" the prompt says may drive signals — a future Feature 05
is expected to call this rather than `list_recent`/`query_history` directly for warm-up.

### 5. Persistence writes never block tick processing — a bounded queue plus one writer thread

`MarketDataBarService` already dispatches ticks and closes bars on the
`EventCoordinator`'s single consumer thread (ADR 0003's threading model). Writing to
SQLite synchronously there would violate "不得阻塞行情 callback" the first time the disk
is slow. Instead, `_handle_closed_bars()` enqueues a `BarRecord` onto a bounded
`queue.Queue` (`put_nowait` — never blocks); a dedicated daemon writer thread drains it
with capped exponential-backoff retry (`_write_with_bounded_retry`, mirroring
`infrastructure.yuanta.backoff.BackoffPolicy`'s shape but reimplemented locally since
application code may not import `infrastructure` — see the layering contracts in
`pyproject.toml`). A full queue or exhausted retries both set a `is_degraded` flag and
publish `BarPersistenceHealthChanged`; `desktop.composition.compute_readiness` surfaces
it as a new "Market data: bar history persistence" readiness row. This satisfies "無法
保證落盤時 readiness 顯示 degraded" without inventing a stronger guarantee ("nothing has
failed yet" is reported as healthy, which is honest, not the same as "guaranteed").

### 6. Duplicates are silently ignored; conflicts are rejected, never silently overwritten; corrections are a separate, audited path

`BarRecordRepository.upsert_closed_bar()` returns one of three outcomes rather than
raising or blindly overwriting: `INSERTED` (first time), `DUPLICATE_IGNORED` (identical
OHLCV already stored — a duplicate `BarClosed` replay, e.g. after a process restart),
`CONFLICT_REJECTED` (different OHLCV already stored — never applied). This is the
concrete mechanism behind "重複 BarClosed...不得建立重複資料" and "晚到 tick 不得默默改寫
已保存的 closed bar". A genuine correction (an operator-run repair procedure) has a
separate method, `apply_correction(record, reason=...)`, which requires an existing row,
bumps `revision`, and writes one row to a `bar_record_revisions` audit table (previous
and new OHLCV, reason, revised time) — `SqliteBarRecordRepository` is the only
implementation that models the audit table; no caller in this codebase invokes
`apply_correction` yet (no repair UI/procedure exists), but the capability and its test
coverage exist per the prompt's explicit "須保存修訂時間與 audit" requirement. Because
continuity/gap state (`continuous_warm_up_bars`, `recorded_range`) is always recomputed
fresh from the repository rather than cached, a future correction is automatically
reflected in the next read — there is no stale cache to invalidate.

### 7. Retention cleanup is a global sweep on the bars table only, run at startup and on daily rollover

`delete_before(cutoff_trading_day)` deletes every `bar_records` row (any
instrument/contract) with `trading_day < cutoff` — not scoped per-contract, since the
two-month window is a single global rule, and it only ever touches this one table
(orders/fills/positions/audit don't live here at all, so "不得刪除" is true by
construction, not by a runtime check). `MarketDataBarService.start()` runs it once
synchronously; `on_clock_tick()`'s existing periodic sweep also checks whether the wall-
clock *date* has changed since the last check and re-runs it if so — an approximation of
"每日交易日切換" using calendar-date rather than true trading-day rollover (which would
require session-boundary-aware logic for no real benefit: retention deleting a few hours
early/late relative to a session boundary is inconsequential, unlike bar-boundary
timing itself). Each run publishes `BarRetentionCleanupCompleted` (cutoff + deleted
count) as the "audit 摘要" the prompt requires.

### 8. Warm-up loading happens on `clear()` *and* on every `BrokerSessionReady` (not just once)

The prompt lists three trigger points — 啟動、重連、切換契約 — for loading persisted
history. `InstrumentSelectionService.switch_to()` already calls `clear()` synchronously
(ADR 0006 decision 9), which covers both "啟動" (the operator always confirms a
selection before the first start, calling `switch_to()`) and "切換契約". "重連" doesn't
go through `switch_to()` — the same `_ActiveContract` survives a reconnect in-memory —
so `_on_session_ready()` (already the `BrokerSessionReady` handler) now also calls the
same `_load_warm_up()` helper `clear()` uses, re-syncing the streak counter and
recent-bars view from the repository's current state on every reconnect, not just once
per process. `_load_warm_up()` always rebuilds from scratch (fresh
`CandleStreakCounter`, fresh `recent_closed` deque) rather than incrementally patching
existing in-memory state, so it's idempotent and safe to call repeatedly.

### 9. Query surface added to `MarketDataBarService`, not exposed as a raw repository on `ServiceContainer`

`continuous_warm_up_bars`, `recorded_range`, `query_history`, `is_persistence_degraded`,
`candle_streak`, and `last_retention_summary` are new public methods on
`MarketDataBarService` — the existing query façade `MarketDataPanel` already talks to
(`is_stale`/`has_gap`/`forming_bar`/`recent_closed_bars`). `BarRecordRepository` itself
is not added to `ServiceContainer`; UI code has no reason to bypass the service. This
also required exposing 紅／黑／十字 streak state for the first time
(`candle_streak()`) — ADR 0006 built `CandleStreakCounter` without any external query
method since nothing needed to observe it yet; "重啟後可還原...紅／黑／十字狀態" is only
a testable/observable guarantee if something can read it back, so this extension adds
the minimal getter and a small status-line display in `MarketDataPanel`.

### 10. `RecordedRange.covers_full_window` is honest about first activation

The prompt requires the UI to say "自 YYYY-MM-DD HH:mm 起開始收錄" and never claim a
full two months when less has accumulated. `RecordedRange` (earliest/latest recorded +
the window's start date at query time) computes `covers_full_window` as
`earliest_at.date() <= window_start` — false for essentially all of the first two months
of real use, which is the documented normal state, not an error condition.

## Consequences

- Production deployment needs a real, writable per-user data directory for
  `market_data.sqlite3` (`%LOCALAPPDATA%/tfx_quant/` by default, or an operator-supplied
  `TradingSettings.market_data_db_path`) — the first genuinely stateful artifact this
  codebase creates outside of vendor-required OCX registration.
  `tests/conftest.py`'s `valid_settings_raw` fixture points every test-built
  `ServiceContainer` at an isolated `tmp_path` database so the test suite never shares
  or accumulates a real on-disk file.
- Feature 05 (strategy signal engine) is expected to call
  `MarketDataBarService.continuous_warm_up_bars()` for entry-gate warm-up, not
  `recent_closed_bars()`/`list_recent()` directly, so it only ever sees a bar sequence
  already verified gap-free.
- No repair/correction UI exists yet — `apply_correction()` is repository-layer-complete
  and tested, but nothing in this codebase calls it. A future repair feature should
  reuse it rather than adding a second way to revise a stored bar.
- The retention sweep's "daily" trigger is wall-clock-date-based, not
  trading-calendar-aware; this is a deliberate simplification (see decision 7) — don't
  "fix" it into a trading-day-boundary-precise trigger without a concrete reason, since
  the prompt's own tolerance here is coarse (bars, not deletion timing, are what must be
  boundary-exact).
