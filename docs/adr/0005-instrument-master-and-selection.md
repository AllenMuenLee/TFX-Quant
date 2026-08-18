# ADR 0005 — Instrument master and selection (Feature 03)

## Status

Accepted (Feature 03).

## Context

Feature 03 needs to: represent 小台指(MXF)/大台指(TXF) contracts (code, month, expiry,
tick size, multiplier, trading session, tradability), let the operator pick an
instrument and contract month (explicit, or auto-resolved near-month with mandatory
confirmation), and safely switch between them — cancelling the old quote subscription,
requerying account status, subscribing the new contract, clearing K-bar/signal state,
and always requiring a fresh manual start afterward. Feature 02 deliberately left the
`Instrument`/`ContractMonth` → vendor quote-symbol translation as an open gap
(`broker_session_gateway_views.py`'s `subscribe()`/`QuoteAdapterPort` both flagged it),
and this ADR is where that gap gets closed.

Two questions had to be answered from the actual vendor documentation, not assumed:

1. **Is there a live vendor "commodity master" API to query?** Re-checked both
   extracted PDFs (`元大BToCAPI格式.pdf`, `元大行情API.pdf`) for every `Func`/
   `UserDefinsFunc` code mentioned. All of them (`FA001`-`FA009` open-interest/
   financial queries, `RA003` position query, `RA004` foreign open-interest) are
   account-state queries, not a product catalog. **No such call exists.**
2. **Can the EasyWin-format vendor symbol be computed from `Instrument`+`ContractMonth`
   by a formula?** The only concrete example in either PDF is `TXFD9,TXFD9/E9` (a
   combo-order example) and `Symb=TXFE9|Scnam=台指期 9 月` (confirms `E9` = September
   1993→...2009's September contract, from a 2009-05-19-dated worked example). Neither
   the standard international futures month-code table (`F G H J K M N Q U V X Z` for
   Jan-Dec) nor TAIFEX's commonly-cited `A-L` table produces `E` for September under
   any consistent reading — the two data points (`D9` paired with `E9` as adjacent
   listed months, `E9` confirmed = September) don't fit a simple linear mapping either.
   **The encoding is Yuanta/EasyWin-internal and not derivable from the available
   documentation.**

The implementation prompt anticipates exactly this possibility: "商品代碼、契約格式、
到期日與可交易月份必須取自元大 API **或受控商品主檔**" (must come from the Yuanta API
*or* a controlled master file) — not "must be computed."

## Decisions

### 1. 商品主檔 is a controlled, versioned JSON file — never a computed formula

`infrastructure/yuanta/instrument_master_repository.py`'s `JsonInstrumentMasterRepository`
loads `InstrumentMasterEntry` records (`domain/instrument_master.py`) from a JSON file
(`instrument_master.example.json` is the bundled seed/example, matching
`desktop/settings.example.json`'s role). Every field that could plausibly change or
that isn't safely derivable — `vendor_symbol`, `tradable`, `expiry_date` — is literal
data an operator maintains, not a formula in code. This directly satisfies the
implementation prompt's "不得把可能變動的代碼規則散落在程式中" (don't scatter
possibly-changing code rules through the codebase): there is exactly one place this
data lives, and it's not Python code.

`expiry_date` specifically is *not* recomputed via a "3rd Wednesday of the contract
month" rule in code, even though that's TAIFEX's normal settlement-date convention and
would have produced plausible-looking values — real trading calendars occasionally
shift around holidays, and a hardcoded rule that's right 95% of the time is worse than
an explicit, auditable data field, given a wrong expiry date here means "allowed to
open a position in an expired/about-to-expire contract."

The bundled `instrument_master.example.json` marks every `vendor_symbol` with a
`-UNCONFIRMED` suffix and a top-level `_comment` warning — it is example/seed data for
`use_mock: true` development, not something to trade real money against. Production use
requires `TradingSettings.instrument_master_path` to point at a file an operator has
confirmed against Yuanta/EasyWin.

### 2. `InstrumentSelectionService` never drives `StrategyStateMachine` itself

The implementation prompt's two switching rules read as contradictory at first: "切換
流程必須先暫停" (the switch flow must first pause) vs. "策略執行中...時禁止切換" (forbid
switching while executing). Resolved by treating "must first be paused" as a
**precondition** `check_switch_allowed()` checks (state already `STOPPED` or
`PAUSED_SAFE`), not an action `switch_to()` performs. Since `StrategyState.RUNNING` is
only reachable via a fresh `STARTING` (`domain/strategy_state.py`, unchanged by this
feature), leaving the machine in whatever paused/stopped state it was already in after
a switch already guarantees "成功後仍需使用者重新啟動" (the user must restart
afterward) without this service needing to force any transition.

`switch_to()` calls `check_switch_allowed()` twice: once before touching anything, and
again after cancelling the old quote subscription — this second call is the literal
"重新查詢帳戶狀態" step, guarding the window between the first check and the actual
switch. Because `BrokerSessionTradeGatewayView.query_open_orders`/`query_positions`
still raise `NotImplementedError` in the real (non-mock) branch (Feature 02's
documented `ReportQuery`/`DealQuery`/`RA003` parsing gap — still open, not this
feature's job to close), that requery legitimately can't be answered yet against a real
broker session; `check_switch_allowed()` catches `NotImplementedError` and turns it into
a blocked switch rather than a silent unsafe assumption. Switching against the mock
gateways works today; switching against the real gateway will start working
automatically once Feature 02/08 close that parsing gap, with no change needed here.

### 3. `StartupSafetyGate.SafetyChecklist` grows two fields, not a separate gate

`instrument_contract_confirmed` (the operator explicitly confirmed the selected/
resolved instrument+contract — required even in AUTO mode, per "必須顯示解析後契約並
要求啟動前確認") and `quote_position_order_consistent` (the subscribed quote and every
known position/open order all agree on instrument+contract, per "行情、持倉、委託三者
不一致時不得啟動") are added to the existing seven-field checklist (now nine) rather
than introducing a second, parallel gate. `StartupSafetyGate.can_enter_running()` was
already the single chokepoint into `Running`; splitting instrument-selection safety
into its own gate would create two places a future feature could forget to check.

### 4. Runtime market-data subscribe/unsubscribe added to `IBrokerSession`/orchestrator

`BrokerSessionOrchestrator` gained `subscribe_market_data(symbol)`/
`unsubscribe_market_data(symbol)` — thin, `READY`-phase-only wrappers around the
existing `QuoteAdapterPort.subscribe`/`unsubscribe`, distinct from the fixed
`market_data_symbols` list used during the startup sequence. The startup capability
calculation (`_current_capabilities()`'s `symbols_subscribed`) was changed from set
*equality* against `market_data_symbols` to a *subset* check, so a runtime subscription
added via Feature 03's switch flow can never retroactively flip `market_data` back to
`False` for symbols beyond the original startup set. `IBrokerSession` and
`MockBrokerSession` both gained the same two methods so
`BrokerSessionQuoteGatewayView.subscribe`/`unsubscribe` (now fully implemented, no
longer raising) can translate `Instrument`/`ContractMonth` → `vendor_symbol` via
`InstrumentMasterRepository` and call through uniformly for mock and real sessions.

## Consequences

- Production deployment now has a real, non-optional dependency: someone with EasyWin/
  Yuanta access must populate a real `instrument_master.json` (confirmed
  `vendor_symbol`s, current `tradable`/`expiry_date`) before `use_mock: false` is safe
  to run against real contracts. This is treated as a feature, not a gap — the
  alternative (guessing the symbol formula) risks silently subscribing to or trading
  the wrong contract.
- Feature 06 (order/fill state machine) can reuse `application.instrument_selection.
  validation.validate_can_open`/`validate_order_price` directly for its own
  pre-submission checks — they take already-fetched data and have no dependency on the
  stateful `InstrumentSelectionService`.
- Feature 04/05 (market data bars, strategy signal engine) must implement a real
  `BarSignalStateStore` (`application/ports/bar_signal_state.py`) and wire it into
  `desktop/composition.py` in place of `NullBarSignalStateStore` — until then, the
  "清空 K 棒／訊號狀態" step is a documented no-op, matching Feature 02's own pattern of
  flagging forward-dependent gaps rather than faking them.
