# Modules

What's in each package under `src/tfx_quant/`. Dependency direction points toward
`domain`; see `docs/adr/0003-layering-and-event-threading-model.md` for how that's
enforced (`import-linter`).

## `domain`

Immutable value types and pure business rules. Stdlib only — no third-party
dependency, no I/O. `Instrument`, `ContractMonth`, `InstrumentMasterEntry` (Feature 03
— 商品主檔), `TradingAccount`, `Side`, `Quantity`/`NetPosition`, `Price`/`Money`,
`Timestamp`, `Bar` (+ `CandleColor` — Feature 04), `Tick`, `TradingCalendar`,
`BarAggregator`/`CandleStreakCounter` (Feature 04 — 60-minute bar aggregation and
red/black/doji streak counting; see `docs/adr/0006-market-data-and-bar-aggregation.md`),
`StrategySignal`, `Position`, `Order`/`ClientOrderId`, `Fill`, `Pnl`, `StrategyState` +
`StrategyStateMachine`. All illegal-state construction raises a `DomainError` subclass
from `domain/errors.py`.

## `application`

- `application/ports/` — `Protocol` interfaces (`Clock`, `IdGenerator`,
  `TradeGatewayPort`, `QuoteGatewayPort`, `IBrokerSession`, `InstrumentMasterRepository`,
  `BarSignalStateStore`, `TradingCalendarRepository`) that infrastructure
  implementations satisfy. `IBrokerSession` (Feature 02) is the Yuanta login/session-
  lifecycle port — richer than, and additive to, `TradeGatewayPort`/`QuoteGatewayPort`;
  see `docs/adr/0004-broker-session-architecture.md`. `InstrumentMasterRepository`/
  `BarSignalStateStore` (Feature 03) are the 商品主檔 lookup and K棒/訊號 reset seams —
  see `docs/adr/0005-instrument-master-and-selection.md`. `TradingCalendarRepository`
  (Feature 04) is the controlled 交易日曆（休市／提早收盤）lookup seam — see
  `docs/adr/0006-market-data-and-bar-aggregation.md`.
- `application/events/` — `Event` shapes and `EventCoordinator`, the single
  serialized event-processing queue. Feature 04 adds `MarketDataTickReceived`,
  `BarClosed`, `MarketDataFreshnessChanged`, `MarketDataGapDetected`/
  `MarketDataGapCleared`.
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
  `docs/adr/0006-market-data-and-bar-aggregation.md`.

Depends only on `domain`.

## `infrastructure`

Real and mock implementations of `application.ports`. `infrastructure/clock.py`
(`SystemClock`), `infrastructure/identity.py` (`UuidIdGenerator`), and
`infrastructure/bar_signal_state.py` (`NullBarSignalStateStore`/
`InMemoryBarSignalStateStore` — Feature 03 test/dev placeholders; the real bar/signal
state is `application.market_data.MarketDataBarService`, Feature 04) are generic.
`infrastructure/market_data/` (Feature 04) — `JsonTradingCalendarRepository`, backed by
`trading_calendar.example.json` (a best-effort, web-search-seeded 2026 TAIFEX holiday
list, explicitly flagged unconfirmed — same posture as the instrument master's
`-UNCONFIRMED` vendor symbols); generic, not Yuanta-specific, since exchange holidays
aren't vendor data. `infrastructure/yuanta/` is the Yuanta-specific adapter — the
**only** package allowed to import vendor COM/OCX types (its
`instrument_master_repository.py`, added by Feature 03, and `market_data_parsing.py`,
added by Feature 04, are exceptions to the *COM* part only — plain parsing/JSON I/O, no
COM dependency, but Yuanta-specific data/wire-format). Feature 01 shipped
`MockTradeGateway`/`MockQuoteGateway` only; Feature 02 adds the real session
(`session_orchestrator.py`, `trade_ocx_adapter.py`, `quote_ocx_adapter.py`,
`ocx_host.py`, `credentials.py`, `preflight.py`, `backoff.py`) plus
`MockBrokerSession`; Feature 03 adds `instrument_master_repository.py`
(`JsonInstrumentMasterRepository`, backed by `instrument_master.example.json`) and
implements `broker_session_gateway_views.py`'s previously-stubbed `subscribe`/
`unsubscribe`; Feature 04 adds `market_data_parsing.py` (raw `OnGetMktAll` field
parsing) and wires it through `quote_ocx_adapter.py`'s new `OnGetMktAll` handler and
`session_orchestrator.py`'s new `handle_market_data_push`. See
`infrastructure/yuanta/README.md` for the vendor API inventory,
`docs/adr/0004-broker-session-architecture.md` for the session architecture,
`docs/adr/0005-instrument-master-and-selection.md` for the instrument master/selection
design, and `docs/adr/0006-market-data-and-bar-aggregation.md` for the market-data/bar
design.

## `persistence`

SQLite storage. Feature 01 only wires `sqlite_connection.create_connection()` —
schema, migrations, and repositories are Feature 14's job.

## `desktop`

The composition root (`composition.py`, `build_services()`/`load_settings()`) and the
wxPython UI shell. `app.py` (the `wx.App`), `readiness_frame.py` (the startup
diagnostics + session-control screen — no order-sending control anywhere; Feature 02
adds a Connect/Disconnect/account-picker for the broker session, which is
session-lifecycle control, not order submission), `instrument_selection_panel.py`
(Feature 03 — instrument/contract pick, AUTO/MANUAL mode, resolved-contract preview and
explicit confirm-and-switch button, embedded in `ReadinessFrame`; also
session-lifecycle-adjacent, not order submission), `market_data_panel.py` (Feature 04 —
forming-bar OHLCV, recent closed bars with red/black/doji marker, last-update time, and
stale/gap badges; a pure display surface, `ReadinessFrame` owns the event subscriptions
and calls its `refresh()`), `__main__.py` (the `python -m tfx_quant.desktop` entrypoint
— also starts/stops `MarketDataBarService`'s background timer alongside the
`EventCoordinator`'s). The only package allowed to depend on everything else; nothing
depends on it.

## `tests`

Mirrors the `src/tfx_quant/` package structure (`tests/domain/`,
`tests/application/`, `tests/infrastructure/`, `tests/desktop/`).
