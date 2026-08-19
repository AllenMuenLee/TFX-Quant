# ADR 0004 — Broker session architecture (Feature 02)

## Status

Accepted (Feature 02, legacy OCX API). **Superseded (2026-08-19, SPARK API pivot)**:
every implementation prompt now mandates the official 元大 SPARK API
(`https://www.yuanta.com.tw/file-repository/content/API/page/index.html`) as the only
valid spec source — the legacy COM/ActiveX OCX pair this ADR originally described is
confirmed **legacy, maintenance-only**. This is a full rewrite, not an addendum: the
whole hosting/threading/registration model changes because SPARK API's shape is
fundamentally different (one `.NET` session object instead of two COM controls). The
original OCX-era content (COM registration, `AtlAxCreateControlEx`, the DLL
search-path/MFC bugs) is preserved in git history for archaeological reference — see
`docs/adr/0001-python-version-and-runtime.md` and
`docs/adr/0002-ui-framework-and-com-hosting.md`'s own superseded-status notes, and
`infrastructure/yuanta/README.md`'s "Legacy OCX API (superseded)" section.

## Context

SPARK API (see ADR 0001/0002's addenda) is a `.NET 8` C# component
(`YuantaSparkAPI.dll`), driven from Python via `pythonnet`. One `YuantaSparkAPITrader`
object provides login, market data, trading, reports, and account queries together —
there is no separate trade/quote control to host, no ProgID/CLSID, no window handle
requirement, no bitness constraint. Every async result (query or push) arrives through
one dispatch point: `OnResponse(intMark, dwIndex, strIndex, objHandle, objValue)`
(`intMark`: 0=系統回應, 1=查詢回應, 2=訂閱推播; `strIndex` names the originating
function, e.g. `'Login'`/`'GetRealReport'`/`'SubscribeStockTick'`).

Facts below were verified directly against the official docs site
(`http://www.yuanta.com.tw/file-repository/content/sparkapi_docs/...`, a client-rendered
SPA requiring browser automation — plain HTTP fetch returns empty) during this session,
not guessed or carried over from the legacy API's naming.

## Decisions

### 1. One adapter, not two — `SparkApiSessionAdapter`/`spark_client.SparkApiClient`

`infrastructure/yuanta/spark_client.py` isolates every `pythonnet`/`clr`/`YuantaOneAPI`
import to one thin wrapper module (constructing `YuantaSparkAPITrader`, exposing
`Open`/`Login`/`LogOut`/`Close`/`Dispose`/`GetRealReport`/`GetFutStoreSummary`/
`SubscribeStockTick`/`UnSubscribeStockTick`). `infrastructure/yuanta/
spark_api_adapter.py`'s `SparkApiSessionAdapter` wraps that client and implements
`session_orchestrator.SparkAdapterPort`, dispatching `OnResponse` by `strIndex` into
`BrokerSessionOrchestrator.handle_*` calls — extracting only primitive values from the
`.NET` objects before crossing into pure-Python code, same "event sink does the
minimum, real processing happens on the orchestrator" split the legacy adapters used.

This replaces the legacy design's two adapters (`YuantaTradeOcxAdapter`/
`YuantaQuoteOcxAdapter`), `ocx_host.py` (OCX window hosting), `com_registration.py`
(per-user COM registration), and `vendor_codes.py` (undocumented `ReqType`/`SetMap`
tables) — all deleted outright, not adapted, since none of it applies to a `.NET`
object with no window/registry footprint.

### 2. Sequencing collapses from five steps to three

Legacy: trade login → order query → fill query → position query → quote login →
subscribe. SPARK API: **one** `Open()`+`Login()` covers trading, market data, and
reports together (`RR_RealReport`, the order/fill push, auto-subscribes on login —
"登入即訂閱", no separate opt-in call). `BrokerSessionOrchestrator`'s new sequence:

登入 → (帳號選擇，若回傳多筆) → `GetRealReport` (回報查詢) → `GetFutStoreSummary`
(庫存查詢) → `SubscribeStockTick` (行情訂閱) → session-ready.

The two safety queries are kept — not because SPARK API requires them before trading
works, but because this codebase's own safety posture (the implementation prompt's
global "no unknown orders/positions at startup" rule) wants them confirmed before
declaring `READY`, same intent the legacy five-step sequence served.

### 3. `SessionCapabilities`'s five flags — what "true" means now

- `login`: `LoginResult.LoginStatus.MsgCode` was `0001`/`00001`.
- `order_reports`: same as `login` — `RR_RealReport` has no separate subscribe step
  (see decision 2), so there's nothing else to wait for.
- `queries`: both `GetRealReport` and `GetFutStoreSummary` query responses received.
- `trading`: `login` AND an account is selected AND `queries`.
- `market_data`: `login` AND every startup-configured symbol's `SubscribeStockTick`
  call succeeded.

Still five independent booleans, per the implementation prompt's explicit "never
collapse `login` into `trading`" rule — even though `login`/`order_reports` now
coincide in practice, they remain separately computed so a future SPARK API revision
that *does* separate them wouldn't require a capability-model change.

### 4. `IBrokerSession`/`LoginRequest` — mostly unchanged shape, changed meaning

`application/ports/broker_session.py`'s `IBrokerSession` Protocol survives essentially
unchanged (same method signatures) — Feature 02's application-layer design held up
across the vendor pivot. Two real changes:

- **`QuoteSession` (day/night quote-port selection) is removed.** The legacy quote
  OCX had two logon ports (日盤/夜盤); nothing in the SPARK API docs (`Open`/`Login`,
  or any 行情 subscribe page) documents an equivalent session-selection concept —
  `SubscribeStockTick` takes no such parameter. Removing it is a confirmed
  simplification, not a guess: absence of a parameter in every relevant docs page is
  itself the evidence.
- **`user_id` is now the full SPARK API account string** (e.g. `F`+17 digits for
  futures), not a "歸戶 ID" that expands to multiple sub-accounts after login. See
  decision 6.

`LoginRequest` deliberately does **not** carry a certificate path — see decision 5.

### 5. Certificate handling is a login-screen concern, not part of `LoginRequest`

On Windows, `Login(Account, Pass)` takes only two parameters — no certificate path (登入
docs page, Windows tab). The docs say a certificate must be imported into the OS
certificate store ahead of time (前言 > 測試環境&正式環境說明: "測試憑證請下載憑證並匯入
至電腦即可使用" / "正式憑證請至網站上申請憑證...將憑證匯入至電腦即可使用") — a one-time
setup step, not part of any individual login attempt. (Linux/Mac's `Login()` signature
*does* take a certificate path + password as its first two args — irrelevant here since
this codebase targets Windows only.)

Per the client's explicit instruction (asked when confirming this rewrite: "the login
screen should let the user provide/import their own certificate"), `desktop/
login_dialog.py` gained a certificate file field + "瀏覽…"/"匯入憑證" controls, backed
by `infrastructure.yuanta.credentials.ensure_certificate_imported` — a `certutil
-importpfx -user` call (no admin rights needed, matching this codebase's existing
posture), with the certificate password piped via stdin rather than a CLI argument (so
it never appears in a process argument list). **This whole mechanism is an
app-level design choice, not a documented SPARK API call** — flagged as such in the
code, and deliberately kept out of `LoginRequest`/`IBrokerSession` since it's not part
of the `Login()` RPC itself.

### 6. Account resolution: `LoginResult.LoginList`, not `OnLogonS.AccList`

The legacy quote/trade OCX returned a semicolon-delimited `AccList` string after one
歸戶-ID login, routinely containing several branch/sub-account entries to choose among.
SPARK API's `Login(Account, Pass)` is called with one specific, fully-qualified account
string already (`F`+分公司代號(7+3)+帳號(7)) — the response's `LoginResult.LoginList`
is structurally still a list (`Count` can in principle be >1), so
`BrokerSessionOrchestrator` keeps the "auto-select if exactly one entry, otherwise
require `select_account()`" logic from the legacy design, just sourced from
`LoginList` instead of `AccList`. In practice this is expected to almost always be a
single entry given the account string is already specific — but nothing in the docs
rules out more, so the safety property (never silently pick among multiple accounts) is
kept rather than assumed away.

`session_orchestrator._parse_login_accounts` reconstructs `TradingAccount(branch_id,
account_no, sub_account="")` from the `F`+17-digit string by splitting the documented
`分公司代號(7+3)+帳號(7)` layout as `branch_id = digits[0:10]`, `account_no =
digits[10:17]` — **this exact split is a best-effort reading of the docs' own
notation, not independently confirmed against a real login response** (no real futures
UAT account was available to this session — see "What's not verified" below). If a
real login response's `Account` field doesn't decompose this way, only
`_parse_login_accounts`/`spark_api_adapter.account_to_spark_string` need correcting —
everything else keys off the reconstructed `F`+account string, not the split fields
independently.

### 7. Threading model unchanged

`.NET` events (`OnResponse`) fire on a CLR-managed thread, not the wx main thread — the
same fundamental shape as the legacy COM callback threading concern. ADR 0003's
`EventCoordinator` design (convert to an internal `Event` immediately, hand off to the
queue, never touch wx widgets directly from the callback thread) is reused unchanged —
only what feeds the coordinator changed (one `OnResponse` dispatch instead of several
COM event-sink methods).

## What's confirmed vs. what's not verified

**Confirmed directly from the official docs site this session** (not inferred from the
legacy API, not guessed): `Open`/`Login`/`Logout`/`Close`/`Dispose` signatures, Windows
vs. Linux/Mac `Login()` argument shapes, account string formats, `OnResponse` dispatch
shape, `GetRealReport`/`GetFutStoreSummary`/`SubscribeStockTick`/`UnSubscribeStockTick`
signatures and field lists, `RR_RealReport`'s "登入即訂閱" auto-subscribe behavior,
`GetKLine`'s TWSE/TWOTC-only restriction (see ADR 0006's addendum), the 期貨報價代碼7xxx
formula (see ADR 0005's addendum), and the documented rate limits (see
`infrastructure/yuanta/README.md`).

**Not verified this session** (no vendor DLL, no .NET 8 SDK, and no real futures
account available in this environment):

- Any real `Open()`→`Login()`→`OnResponse` round-trip — `spark_client.py`/
  `spark_api_adapter.py` are written strictly from the documented signatures, never
  executed against a live server. Unlike Feature 02's original legacy-OCX build (which
  did get a real end-to-end round-trip against the vendor's test server), this rewrite
  has **zero real execution evidence** — treat it as a from-the-docs first draft until
  someone with a real environment (.NET 8 SDK, the vendor DLL, and either the
  documented securities UAT test account or a real futures account) runs it.
- Whether `LoginResult.LoginList` ever actually returns more than one entry for a
  single `Login(account, pass)` call (see decision 6) — the docs' worked examples all
  show `Count: 1` matching the input account exactly.
- The exact `分公司代號(7+3)+帳號(7)` split within a full `F`+17-digit account string
  (see decision 6) — a defensible reading of the docs' notation, not confirmed against
  a real response.
- Whether a real futures UAT/sandbox account exists at all: the 登入 docs page's own
  Python example shows the futures `Login(...)` call **commented out** with
  placeholder-looking credentials, while the 帳務 > 期貨庫存總表查詢 page's example
  shows an *active* futures login with different placeholder-looking credentials —
  inconsistent across pages. Don't assume either way; confirm with the client's 營業員
  before attempting a real futures-account login.
- Whether `SubscribeStockTick`'s synchronous `bool` return is a reliable
  accept/reject signal on its own, or whether (like `Login`) the *real* result only
  ever arrives via a subsequent `OnResponse` query-response — `spark_api_adapter.py`
  currently treats a synchronous `False` as sufficient to raise
  `MarketDataSubscriptionError`, a simplification flagged here rather than silently
  assumed.
