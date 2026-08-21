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

## Extension (2026-08-19): vendor `GetKLine` backfill for local gaps — SUPERSEDED 2026-08-21

**Superseded in full by the "Extension (2026-08-21)" section below.** The implementation
prompt was rewritten to require `yfinance` (a third-party data source) instead of the
vendor's `GetKLine` — see that section for why and for the current design. This section
is kept as a historical record of the reasoning that led to the vendor path being tried
first (and why it was a stated, flagged product decision even at the time, not a
confident integration) — none of the classes/ports/decisions numbered 11-14 below exist
in the codebase any more (`BarHistoryBackfillService`, `BarDataSource`, and
`domain/bar_history_backfill.py` all still exist under the same names, but rebuilt
against `yfinance` instead — see the new section's decisions for their current shape).

A second, later revision of the same implementation prompt added one more requirement:
if the rolling two-month window isn't fully covered by what this process has recorded
itself, query the vendor's own official historical-price API and write the result into
the local database. The prompt named a specific docs URL for this
(`行情/行情報價表訂閱/index.html`).

**That URL turned out not to be a historical query at all.** Reading it directly (per
this project's own "official docs override the prompt" rule) showed it documents
`SubscribeWatchlistAll` — a real-time push subscription with no date-range parameters.
The only genuine historical query in the whole 行情 section is `GetKLine`, and its own
docs attach "註1：僅提供台股上市櫃商品查詢" (TWSE/OTC-listed securities only) to its
`MarketType` parameter — on its face, futures are excluded, matching this codebase's
pre-existing, already-verified conclusion (`[[yuanta-spark-api-pivot]]`, ADR 0006
decision 7) that no vendor historical/K-bar query exists for futures. This was surfaced
to the user rather than silently implemented or silently dropped; the user's explicit
decision was to call `GetKLine` for futures anyway (`MarketType=TAIFEX`, which the docs'
own `enumMarketType` reference does define as a valid member, alongside TWSE/TWOTC/many
foreign exchanges). This is a **stated product decision to go against the endpoint's own
documented restriction**, not a claim that the restriction note was found to be wrong —
every write this path produces is tagged with a distinct source
(`BarDataSource.BACKFILLED_FROM_YUANTA_KLINE`, never conflated with
`AGGREGATED_FROM_YUANTA_REALTIME`) specifically so this remains honestly traceable if the
vendor's restriction turns out to be real and every call silently returns nothing.

### 11. `BarHistoryBackfillService` is a second, independent writer — never touches a day that already has a local bar

A new application service (`application/market_data/bar_history_backfill_service.py`),
not a growth of `MarketDataBarService`, triggers on `BrokerSessionReady` (learns the
account) and `InstrumentSwitchCompleted` (learns the active instrument/contract); once
both are known, it runs entirely on its own background thread — never the
`EventCoordinator` dispatch thread, since a full run makes several sequential blocking
`GetKLine` calls. It computes the rolling window's trading days
(`TradingCalendar.trading_days_between`), diffs against `BarRecordRepository.list_recent`
to find days with *zero* local bars (day-level, not bar-level — cheaper and sufficient
since `GetKLine` is queried per calendar-date range anyway), and only ever queries/writes
for those days. A day with any local bar — live-aggregated or previously backfilled — is
never re-queried, and `upsert_closed_bar`'s existing dedup/conflict semantics are a second
line of defense on top of that first, coarser check.

### 12. Vendor timestamp resolution assumes open-time labeling, exact match only, never snapped

The `GetKLine` docs never state whether `KLine.TimeStamp` is an open or close label for
intraday periods. This codebase's own `Bar.start` is always an open-time label (see
`domain/bar.py`), so `TradingCalendar.boundary_for_open()` (new) requires a vendor bar's
timestamp to *exactly* equal one of `bar_boundaries()`'s own open outputs before it's
accepted; anything else — off-grid, or the close-label interpretation being correct after
all — is dropped, not snapped to the nearest boundary and not guessed at. This is a
stated assumption, not a verified fact (no real vendor login has ever exercised this
path); a future session that gets real API access should verify it before relying on
backfilled data for anything signal-relevant.

### 13. Query chunking respects the vendor's own per-call limits; each call paced at ≤1/sec

`domain/bar_history_backfill.py`'s `chunk_consecutive_days()` (pure, exhaustively tested)
groups missing trading days into the minimum number of ≤5-calendar-day spans — the docs'
own K線種類查詢限制 table for 60分k. `BarHistoryBackfillService` sleeps between chunks at
the vendor's separately-documented `GetKLine` rate cap (≤1/sec, distinct from the general
3/sec quote/account cap). A vendor call that raises `HistoricalPriceQueryError` (rejected
call, timeout, vendor error) or returns bars that all fail boundary resolution simply
leaves that chunk's days as gaps — retried, if at all, on the next trigger (a fresh
reconnect or contract switch), never looped synchronously here.

### 14. `HistoricalPriceQueryPort` blocks its caller thread; `SparkHistoricalPriceQueryAdapter` bridges the vendor's async `OnResponse` with a single-slot queue

Unlike every other query on `TradeGatewayPort`/`QuoteGatewayPort` (which read already-
synced local state), this port's `query_60m_kline()` genuinely blocks until the vendor's
`OnResponse` fires or a timeout elapses — a deliberate, documented exception to this
codebase's usual "ports are boring sync interfaces over already-current state" shape,
justified by `BarHistoryBackfillService` already running its own dedicated background
thread. `spark_api_adapter.py`'s `SparkApiSessionAdapter` stays the *only* subscriber
registered on the vendor's one `OnResponse` .NET event (new `bind_kline_handler()`/
`request_kline()` methods, dispatched by `strIndex == 'GetKLine'` alongside the existing
session-bring-up dispatch) — `SparkHistoricalPriceQueryAdapter` wraps it rather than
subscribing a second handler directly, and serializes calls behind one lock (matching the
≤1/sec cap) using a single-slot `queue.Queue`, draining any stale leftover before each new
call to reduce (not fully eliminate) a late-response-mismatch race. Mock mode
(`use_mock: true`) wires `MockHistoricalPriceQuery`, which always returns an empty
result — mock mode has no vendor session to query, so every range simply stays a gap
rather than inventing fake historical bars.

## Extension (2026-08-21): `yfinance` backfill replaces vendor `GetKLine`

The implementation prompt was rewritten (`implementation prompt/04-market-data-and-60m-
bars/two-month-bar-history-implementation-prompt.md`) to require the third-party
`yfinance` Python package as the backfill source instead of the vendor's `GetKLine` —
the previous extension's own honesty caveats (unverified futures coverage, an explicit
against-the-docs product decision) motivated the switch to a source this codebase can at
least isolate cleanly and test without a real vendor login. `BarHistoryBackfillService`
keeps its name and its "second, independent writer" role but is substantially rebuilt.

### 15. Gap detection is bar-level, not day-level

Unlike the superseded `GetKLine` path (which treated a trading day with *any* local bar
as fully covered — cheaper, since the vendor call was per-calendar-date-range anyway),
this implementation prompt explicitly requires gaps be computed "依預期交易時段與
canonical bar identity". `BarHistoryBackfillService._expected_closed_boundaries()`
enumerates every canonical 60-minute open-boundary the rolling window expects (via
`TradingCalendar.bar_boundaries()` per session, per trading day — the day session and,
where configured, the night session), excludes anything not yet closed as of `now`, and
diffs the result against `BarRecordRepository.list_recent()`'s existing `bar.start`
values. A day this process was only *partially* connected for (some hours aggregated
live, others missing) is now correctly detected and backfilled hour-by-hour, not skipped
wholesale.

### 16. Ticker mapping is a new controlled port, deliberately shipped empty

`application.ports.yahoo_ticker_mapping.YahooTickerMappingRepository` (backed by
`infrastructure.market_data.yahoo_ticker_mapping_repository.
JsonYahooTickerMappingRepository`, reading `yahoo_ticker_mapping.example.json`) mirrors
`InstrumentMasterRepository`/`TradingCalendarRepository`'s "controlled JSON, never
guessed" pattern. Unlike `futures_quote_symbol()` (a documented Yuanta encoding), no
public spec maps a TAIFEX futures contract month to a Yahoo Finance ticker — and this
session found no way to verify one exists at all (no live network path to Yahoo Finance
from this sandboxed environment). The bundled example file's `mappings` array is
therefore deliberately empty, not populated with an unverified guess: `get()` returning
`None` makes `BarHistoryBackfillService` leave every bar for that contract as a gap and
report `is_degraded()` — an honest "not configured yet" state, not a silent failure.

### 17. `interval="1h"`, `auto_adjust=False`, and every other price-affecting kwarg pinned explicitly

`infrastructure.market_data.yfinance_history_adapter.YfinanceHistoryQueryAdapter` reads
directly off this environment's actually-installed `yfinance` package source (its own
module docstring records exactly which file/line was read) rather than assumed API
shape, per this project's "no third-party API may be invented" rule. `auto_adjust`
genuinely defaults to `True` in the installed version — the opposite of what "預設使用未
自動調整的價格" requires — so every `Ticker.history()` kwarg that affects price/behavior
(`interval`, `auto_adjust`, `actions`, `prepost`, `repair`, `keepna`) is pinned in one
explicit `_HISTORY_KWARGS` dict, never left to a package default.

### 18. Boundary alignment, forming-bar rejection, and OHLCV validation are unchanged in spirit from the vendor path

`TradingCalendar.boundary_for_open()` (generic — never renamed for this extension, since
it was never actually vendor-specific) still requires an incoming bar's open timestamp to
*exactly* match a canonical boundary; still nothing is snapped or guessed. New this
extension: an explicit `boundary_close.value > now.value` check rejects a bar whose
canonical close is still in the future relative to the backfill run's own clock — the
"尚未收盤的最後一根 bar" handling the rewritten prompt calls out by name (the vendor path
never needed this, since `GetKLine`'s vendor-side "已收盤" semantics were assumed to
already exclude forming bars — an assumption this codebase has no way to verify for
`yfinance` either, so the check is enforced locally instead of trusted from the source).

### 19. Local-vs-yfinance OHLCV conflicts get a dedicated audit table and block warm-up for that trading day

The rewritten prompt adds a requirement the vendor extension never had: "相同 identity 的
OHLCV 若在本機與 yfinance 間衝突，保存衝突 audit 與兩方摘要...並阻擋該區段驅動訊號".
`domain.bar_record.BarConflictAudit` (existing + incoming `BarRecord`, detected-at) is
written via a new `BarRecordRepository.record_conflict()` method whenever
`upsert_closed_bar()` returns `CONFLICT_REJECTED` — `SqliteBarRecordRepository` gets a new
`bar_backfill_conflicts` table for it (append-only, mirrors `bar_record_revisions`'
shape). Rather than teach `continuous_segments()` (a pure function with no repository
access) about conflicts, `MarketDataBarService.continuous_warm_up_bars()` now also calls
a new `list_conflicted_trading_days()` query and filters those days out of the record set
*before* computing continuity — a conflicted day is treated as if none of its bars exist
for warm-up purposes, which naturally breaks the continuity chain there without changing
`continuous_segments()`'s own contract at all. Because gap detection is bar-level
(decision 15) but the backfill service still queries/writes with a small ±1-day pad
around each missing-day chunk (the prompt's own "允許為了 API 邊界與連續性檢查在缺口兩側
多取少量資料" allowance), a day that already has full local coverage can still be
re-queried at a chunk boundary — which is precisely how a real conflict would ever be
discovered in the first place, not a wasted query.

### 20. Bounded retry/backoff lives in the adapter, not the service; unknown errors fail fast, never loop forever

`YfinanceHistoryQueryAdapter._run_with_bounded_retry()` retries only a best-effort set of
transient-failure types (`ConnectionError`/`TimeoutError`/`OSError`/
`yfinance.exceptions.YFRateLimitError`/`requests.exceptions.RequestException` — imported
defensively, since this exact exception hierarchy has never been exercised against a real
Yahoo Finance endpoint from this environment either) with capped exponential backoff; any
other exception is wrapped into `YahooHistoryQueryError` and raised immediately, no
retry. `BarHistoryBackfillService` itself never retries a failed chunk synchronously —
same "leave it as a gap, retried only on the next trigger" posture the superseded vendor
path already established (decision 13 there).

### 21. Trigger points: startup/reconnect (`BrokerSessionReady`), switch (`InstrumentSwitchCompleted`), and now also daily rollover

The rewritten prompt lists a fourth trigger the vendor-path version didn't implement
explicitly: "每日交易日切換". `BarHistoryBackfillService` gained its own `start()`/`stop()`
lifecycle and a small internal timer (mirroring `MarketDataBarService`'s daily-retention-
check shape) — `on_clock_tick()` re-triggers a run whenever the wall-clock *date* has
changed since the last run, same wall-clock-date approximation of "trading-day rollover"
ADR decision 7 already accepted for retention cleanup. `yfinance` needs no account/login
at all (public data), so — unlike the vendor path, which used `BrokerSessionReady` to
learn the `TradingAccount` a `GetKLine` call required — this trigger exists purely to
re-check gaps after a Yuanta reconnect, not because the backfill itself needs anything
from the broker session.

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
  already verified gap-free (which now also excludes any trading day with an unresolved
  yfinance conflict — see the 2026-08-21 extension's decision 19).
- No repair/correction UI exists yet — `apply_correction()` is repository-layer-complete
  and tested, but nothing in this codebase calls it. A future repair feature should
  reuse it rather than adding a second way to revise a stored bar. The same is true of
  the new `bar_backfill_conflicts` audit table — it is written to and queried from, but
  no UI surfaces its contents beyond the readiness screen's pass/fail row yet.
- The retention sweep's "daily" trigger is wall-clock-date-based, not
  trading-calendar-aware; this is a deliberate simplification (see decision 7) — don't
  "fix" it into a trading-day-boundary-precise trigger without a concrete reason, since
  the prompt's own tolerance here is coarse (bars, not deletion timing, are what must be
  boundary-exact). `BarHistoryBackfillService`'s new daily-rollover trigger makes the
  same simplification for the same reason.
- **No confirmed Yahoo Finance ticker exists for any TAIFEX futures contract as of this
  writing.** The bundled `yahoo_ticker_mapping.example.json` ships with an empty
  `mappings` array, so a fresh install's backfill will find nothing to query and report
  `is_degraded()` until an operator supplies a real, individually-confirmed mapping —
  this is the expected, honest state, not a bug to silently work around by guessing a
  ticker or substituting a continuous-contract alias.
- The `yfinance` adapter (decisions 11–17 above) has never been exercised against a real
  Yahoo Finance HTTP endpoint from this environment (no live network path to Yahoo
  Finance available here), and this specific Python virtual environment cannot even
  `import pandas`/`import yfinance` at all (a 32-bit interpreter with a `pandas`/`numpy`
  binary-wheel ABI mismatch — see `infrastructure/market_data/
  yfinance_history_adapter.py`'s own docstring). Every parameter/behavior claim in this
  adapter was verified by reading the actually-installed `yfinance` package's source
  directly (not assumed from memory), and its `pandas`-independent logic (row-level
  parsing/validation, the retry/backoff loop) is unit-tested; the `pandas.DataFrame`-
  shaped normalization path (`_normalize_dataframe`) is not exercised in this
  environment and is explicitly marked skipped, not silently omitted, in
  `tests/infrastructure/test_yfinance_history_adapter.py`. Treat this as a solid,
  source-faithful first draft that degrades safely to "nothing filled, gaps stay gaps"
  if any assumption about `yfinance`'s real HTTP behavior is wrong — not as a confirmed
  working integration — until someone with real network access runs it end-to-end.
