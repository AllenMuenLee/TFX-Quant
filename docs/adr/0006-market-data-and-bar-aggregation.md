# ADR 0006 — Market data and 60-minute bar aggregation (Feature 04)

## Status

Accepted (Feature 04).

## Context

Feature 03 left two explicit forward gaps. `broker_session_gateway_views.py`'s
`QuoteGatewayPort.subscribe()` registers for a symbol's market data via `AddMktReg`, but
nothing in the codebase has ever consumed the resulting price pushes. And ADR 0005 says
outright: "Feature 04/05 must implement a real `BarSignalStateStore`
(`application/ports/bar_signal_state.py`) and wire it into `desktop/composition.py` in
place of `NullBarSignalStateStore`" — until now that "清空 K 棒／訊號狀態" step
(`InstrumentSelectionService.switch_to()`'s call to `bar_signal_state_store.clear()`) was
a documented no-op.

The implementation prompt requires getting two things confirmed before writing any
aggregation logic — "先取得客戶確認的 60 分 K 切點與 API 時戳定義" (get the 60-minute cut
points and API timestamp semantics confirmed first) and "不得自行以整點猜測" (never guess
the boundaries) — because a wrong bar boundary silently corrupts every downstream signal.
Two more genuine vendor-documentation gaps surfaced while building this, and are handled
the same way Feature 02/03 handled theirs (ADR 0004/0005): flagged explicitly rather than
guessed.

## Decisions

### 1. `Bar.start` is the label (open time), not the close time — confirmed with the user

The prompt's own worked example only makes sense one way: "08:45、09:45 的已收 K 可供畫面
顯示...但建倉閘門不得放行；最早 10:45 才可判斷建倉." If bars were labeled by close time,
only the 09:45 bar (covering 08:45–09:45) would be closed before the 10:45 gate — the
prompt says *both* 08:45 and 09:45 are already-closed bars. That's only consistent if
"08:45" and "09:45" are **open-time labels**: the 08:45 bar spans [08:45, 09:45) and
closes at 09:45; the 09:45 bar spans [09:45, 10:45) and closes at 10:45 — the first bar
the entry gate may act on. Confirmed explicitly with the user this session rather than
assumed. `domain/bar.py`'s module docstring states this plainly, per the prompt's own
"記錄清楚" instruction — `Bar.start` is the label, `Bar.end` is the close time.

### 2. 60-minute cut points are session-start-anchored, computed at runtime — never hardcoded

Every `InstrumentMasterEntry`'s `day_session_start`/`day_session_end` (08:45–13:45) and
`night_session_start`/`night_session_end` (15:00–05:00) are exact hour multiples apart
(5 hours, 14 hours), so `domain/trading_calendar.py`'s `TradingCalendar.bar_boundaries()`
walks hourly from session start to session end and produces clean, non-overlapping bars
with no partial bar in the normal case — confirmed with the user rather than assumed.
`bar_boundaries()` never hardcodes a clock literal; every boundary is derived from
whichever `InstrumentMasterEntry`/`TradingCalendar` data it's given, satisfying "不得自行
以整點猜測" by construction — the cut points fall out of already-controlled data, they
aren't a separate guess layered on top.

A day-session-only early-close override truncates the final boundary instead of skipping
it entirely — `bar_boundaries()` clamps the last bar to the (possibly overridden) session
end, so an early close still emits one shorter-than-60-minute closing bar rather than
leaving a bar forming forever. Early closes are not applied to a session that crosses
midnight (the night session): TAIFEX's historical early closes shorten the day session,
and there's no precedent — or seed data — for a night-session equivalent.

### 3. Holiday/early-close dates are controlled JSON config, not computed — seeded best-effort

No vendor API (trading or quote) documents an exchange holiday calendar — confirmed by
re-checking every `Func`/`UserDefinsFunc` and OCX method in both extracted PDFs, same
methodology ADR 0005 used for the vendor-symbol gap. So, mirroring
`instrument_master.example.json`'s precedent exactly:
`infrastructure/market_data/trading_calendar.example.json` is a version-controlled,
operator-maintained file (`JsonTradingCalendarRepository` loads it), never a computed
rule. Its holiday list was seeded via a web search of 2026 TAIFEX/TWSE holiday calendars
(cross-referencing several broker/financial sites, verified for weekday-vs-weekend
correctness with `datetime.date.weekday()`, not for the underlying holiday facts
themselves) — the file's `_comment` explicitly flags it as unconfirmed against TAIFEX's
official calendar, exactly like the instrument master's `-UNCONFIRMED` vendor symbols.
No 2026 early-close date is known in advance, so `early_closes` ships empty; the schema
and the `TradingCalendar.session_end_for()`/truncated-final-bar mechanism both exist and
are tested regardless.

### 4. `TolMatchQty` substitutes for an undocumented per-trade sequence number

The acceptance criteria calls for validating a tick's "序號" (sequence number), but
`OnGetMktAll`'s 19 documented fields (`元大行情API.pdf` 三.1, cross-checked against the
live type library — see `infrastructure/yuanta/README.md`) have no such field.
`TolMatchQty` (total traded volume since session open) is the only field that is
strictly non-decreasing per symbol, so `domain/tick.py`'s `Tick.cumulative_volume`
carries it through as the ordering/dedup key: `domain/bar_aggregator.py`'s
`BarAggregator.on_tick()` drops any push whose `cumulative_volume` doesn't strictly
exceed the forming bar's last-seen value (a duplicate or late/out-of-order arrival), and
drops any push timestamped before the most recently *closed* bar's end outright (closed
bars are emitted exactly once and never amended). This is flagged as a substitution for
an undocumented field, not a confirmed vendor guarantee — the same honesty level ADR
0004 already applied to `ReqType`/`SetMap`.

### 5. `MatchTime`'s digit format is parsed defensively, not assumed

`元大行情API.pdf` states only "char\* MatchTime 成交時間" — no digit count.
`infrastructure/yuanta/market_data_parsing.py` accepts 6 digits (HHMMSS) or 9 digits
(HHMMSS + 3-digit milliseconds) and rejects anything else as malformed, dropped rather
than raised into the COM callback dispatch path. Unverified against a live push this
session (no real vendor connection was available) — the same open gap
`quote_ocx_adapter.py`'s `OnMktStatusChange`-firing already carries from Feature 02.

### 6. A bar period with zero ticks is never synthesized

`BarAggregator.on_clock(now)` advances the aggregator's boundary tracking with no tick
required (so a forming bar with real trades still closes promptly during a lull, and so
staleness is detectable), but it never fabricates OHLC data for a period nothing traded
in — there is no honest "open"/"close" price for a bar with zero observed trades. A
no-trade interval simply produces no `Bar`; `CandleStreakCounter` (and, later, Feature
05's actual signal logic) only ever sees bars that really traded. This is simpler than
carrying the previous close forward as a synthetic flat/doji bar, and avoids inventing
data this codebase has no honest way to produce.

### 7. No confirmed historical/tick-replay mechanism — every fresh `BrokerSessionReady` flags a gap

The quote OCX's type library exposes an entirely undocumented "Tick subsystem"
(`AddTickReg`/`GetTickRangeData`/`OnGetTickData`/etc. — `infrastructure/yuanta/
README.md` already flagged this as unused, with no known field layout). Guessing its
shape would repeat exactly the mistake ADR 0005 refused to make for `vendor_symbol`, so
Feature 04 does not use it. Consequence: this codebase has no way to reconstruct a
forming bar's missed ticks after a startup or reconnect. `MarketDataBarService`
subscribes to `BrokerSessionReady` (fired identically on first start and on every
post-reconnect retry — see `session_orchestrator.py`'s `_complete_session()`) and
publishes `MarketDataGapDetected` for the active contract immediately; the gap only
clears (`MarketDataGapCleared`) once one bar has closed cleanly end-to-end afterward.
This makes the acceptance criterion's "若歷史與即時資料有 gap...暫停訊號產生並告警"
concrete without fabricating a backfill this codebase cannot honestly provide.

### 8. Detects and publishes; does not itself enforce

`MarketDataFreshnessChanged`/`MarketDataGapDetected`/`MarketDataGapCleared` are computed
correctly and published reliably, but nothing in this feature blocks an order or pauses
a strategy — Feature 06 (orders) and Feature 05 (strategy signals) don't exist yet in
this codebase. This mirrors `BrokerSessionInvalidated`'s existing documented split
("this is the safe-pause hook... Feature 02 only guarantees this fires reliably"). The
`MarketDataBarService.is_stale()`/`has_gap()` query methods and the published events are
the seam those future features are expected to gate on.

### 9. `MarketDataBarService` is the real `BarSignalStateStore` — learned via `clear()`, not a separate event subscription

`InstrumentSelectionService.switch_to()` already calls `bar_signal_state_store.clear
(instrument, contract)` synchronously, with the *new* selection, immediately before
publishing `InstrumentSwitchCompleted`. Since `MarketDataBarService` already holds an
`InstrumentMasterRepository`, `clear()` alone — a direct synchronous call, not an
asynchronous event — is enough to resolve the new contract's session times and vendor
symbol and start tracking it fresh. This avoids a subscribe-to-`InstrumentSwitchCompleted`
path that would otherwise race against `clear()` over which one "wins" first.

## Consequences

- Production deployment now also needs a real, operator-confirmed
  `trading_calendar.json` before `use_mock: false` is safe to run — the bundled example
  is seeded best-effort, explicitly flagged, same posture as the instrument master's
  vendor symbols (ADR 0005).
- Feature 05 (strategy signal engine) is expected to subscribe to `BarClosed` (or query
  `MarketDataBarService.forming_bar`/`recent_closed_bars`) for its 08:45/09:45/10:45+
  entry-gate logic, and to gate signal generation on `has_gap()`/`MarketDataGapDetected`
  per decision 8.
- Feature 06 (order/fill state machine) and Feature 09 (connectivity/safe-pause) are
  expected to gate order submission on `MarketDataBarService.is_stale()`/
  `MarketDataFreshnessChanged`, per decision 8 and the acceptance criterion "資料 stale
  時不得送出新委託."
- `OnGetMktAll` wiring (parsing, the event sink, `BrokerSessionOrchestrator.
  handle_market_data_push`) is unit-tested against fixture pushes but — like the rest of
  the quote OCX's event-firing behavior — not verified against a live vendor connection
  this session; see decisions 4/5's flagged gaps.
