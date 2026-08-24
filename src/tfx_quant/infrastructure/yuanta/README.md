# infrastructure.yuanta — vendor API inventory

**2026-08-19: this codebase now targets 元大 SPARK API exclusively** — every
implementation prompt was rewritten to mandate the official online docs
(`https://www.yuanta.com.tw/file-repository/content/API/page/index.html` and its docs
site) as the only valid API spec source. The legacy COM/ActiveX OCX pair this file used
to document is confirmed **legacy, maintenance-only** on the vendor's own portal
(`YuantaOneAPI_Com.zip`, listed alongside Delphi/WPF). See `docs/adr/0001-python-
version-and-runtime.md`, `0002-ui-framework-and-com-hosting.md`, and
`0004-broker-session-architecture.md` for the full rewrite rationale — this file is the
quick-reference API inventory.

## SPARK API

A **.NET 8 C#** component (`YuantaSparkAPI.dll`), driven from Python via `pythonnet`.
Cross-platform (Windows/Linux/macOS), both bitnesses on Windows (win-x64/win-x86) — no
bitness constraint, unlike the legacy quote OCX. Official docs site (a client-rendered
SPA — plain HTTP fetch returns empty; needs a real browser to read):
`http://www.yuanta.com.tw/file-repository/content/sparkapi_docs/...`.

Vendor deliverable (the downloaded component zip, containing `YuantaSparkAPI.dll` and
its sibling `.dll`/`.so`/`.dylib` files plus `FunctionList.xls`) is proprietary,
installed locally, and gitignored — never committed. Conventional local path:
`C:\Yuanta\SparkAPI` (`infrastructure/yuanta/spark_client.py`'s
`default_dll_directory()` — a codebase convention, not vendor-mandated). Local log
files default to `C:\Yuanta\YuantaSparkAPI\Log` (Windows), kept 30 days.

### Session lifecycle (基礎)

```python
objYuantaSparkAPI = YuantaSparkAPITrader()
objYuantaSparkAPI.SetLogType(enumLogType.COMMON)
objYuantaSparkAPI.OnResponse += on_response  # one unified callback, see below
objYuantaSparkAPI.Open(enumEnvironmentMode.UAT)  # or .PROD
objYuantaSparkAPI.Login(account, password)  # Windows: 2 args only
```

- `Login(Account: str, Pass: str) -> bool` (Windows) — `Account` is the full account
  string, not a "歸戶 ID": 證券 `S`+分公司代號(4)+帳號(7) (11 chars), 期貨
  `F`+分公司代號(7+3)+帳號(7) (17 chars, e.g. `FF021000P001234567`). This codebase only
  ever logs in with `F`-prefixed futures accounts. The `bool` return only means "call
  accepted" — the real result (`LoginResult`) arrives via `OnResponse` with
  `strIndex == 'Login'`.
- **No certificate path parameter on Windows.** A certificate must be imported into the
  OS certificate store beforehand (前言 > 測試環境&正式環境說明: "...匯入至電腦即可使
  用") — see `credentials.ensure_certificate_imported` and `desktop/login_dialog.py`'s
  certificate import controls. (Linux/Mac's `Login()` *does* take a cert path + cert
  password as its first two args — irrelevant here, Windows-only codebase.)
- `OnResponse(intMark, dwIndex, strIndex, objHandle, objValue)` — the **one** callback
  covering every async result. `intMark`: `0`=系統回應, `1`=查詢回應, `2`=訂閱推播.
  `strIndex` names the originating function (`'Login'`, `'GetRealReport'`,
  `'SubscribeStockTick'`, ...); `objValue`'s shape depends on `strIndex`.
- `LogOut()`/`Close()`/`Dispose()` — session teardown.
- Rate limits (前言 > 使用限制說明, load-bearing for any future throttling work):
  repeat login on an already-logged-in account is rejected; failed-login retry capped
  at 1/4s; ≤10 subscribe calls/sec/FunctionID, ≤200 symbols/subscribe call, ≤3
  quote/account calls/sec/FunctionID (`GetKLine` excluded, capped at 1/sec separately),
  ≤10 trade calls/sec/FunctionID, ≤30 orders/call; per-account ≤10 concurrent
  connections, ≤1000 logins/day, ≤3000 total subscribed symbols, 1200/600/3000
  calls-per-minute for quote/account/trade categories (exceeding → 1-min pause; 10
  pauses/hour → API access revoked for the account).

### Futures trading (交易 > 國內期貨下單) — Feature 06's job, not wired up yet

`SendFutureOrder(LoginAcno, List[FutureOrder], lng=Normal) -> bool`. Cancel/modify
reuse this same method with a different `FunctionCode` on `FutureOrder` (00=新單,
04=取消, 05=改量, 07=改價) — no separate cancel/modify method. Key `FutureOrder`
fields: `Identity` (caller-assigned correlation id), `Account`, `OrderNo` (blank for
new), `TradeDate` ("yyyy/MM/dd"), `CommodityID1` (order-side product code, e.g.
`"FITX"` for TXF — **a separate code space from the real-time quote symbol**, see
below), `SettlementMonth1` (int `YYYYMM`), `Price`, `OrderQty1`, `BuySell1` ("B"/"S"),
`OpenOffsetKind` ("0"=新倉/"1"=平倉/"2"=自動), `OrderType` ("1"=市價/"2"=限價/"3"=範圍
市價), `OrderCond` (ROD/FOK/IOC). Result arrives via `OnResponse` with
`strIndex == 'SendFutureOrder'`, `objValue.ResultList[i].ReplyCode` (0=success).

### Futures quote-symbol encoding (前言 > 期貨報價代碼7xxx變更規則) — now a real formula

`<root(3 chars)><month-code(1 char)><year-digit(1 char)>`. Month code:
`"1ABCDEFGHIJKL"[month]` (near-month alias `1`, then 一月=A...十二月=L); year-digit is
the calendar year's last digit. Worked example: `2021年台指期6月:TXFF1`. Implemented as
`domain.instrument_master.futures_quote_symbol()`, tested against that exact example.
This resolves the previous `-UNCONFIRMED` placeholder problem for `vendor_symbol` in
`instrument_master.example.json` — see `docs/adr/0005-*.md`'s addendum.

**`order_commodity_code` (`SendFutureOrder.CommodityID1`) is a separate, unconfirmed
code space** — only TXF's `"FITX"` is confirmed (from the 國內期貨下單 docs page's own
worked example); other instruments need confirmation against the vendor's
`FunctionList.xls` before Feature 06 can place orders for them.

### Market data — not part of this API

This codebase has no market-data path through the SPARK API at all: no
`SubscribeStockTick`, no `GetKLine`, no tick/quote pushes of any kind. Every OHLCV bar —
both the near-real-time bar builder and the two-month history backfill — comes from the
third-party `yfinance` package instead (see `infrastructure/market_data/
yfinance_history_adapter.py`, `application/market_data/bar_service.py`, and
`implementation prompt/00-spark-to-futures-api-migration/implementation-prompt.md`). An
earlier revision of this codebase did wrap `SubscribeStockTick`/`GetKLine` here; both were
deleted once the implementation prompt was rewritten to forbid any Yuanta/SPARK
market-data path, not left in place unused.

### Reports (回報)

`RR_RealReport` — order/fill push, **auto-subscribed on login** ("登入即訂閱，結果請從
回應事件OnResponse接收" — no separate subscribe call). Arrives via `OnResponse` with
`intMark == 2`, `strIndex == 'RR_RealReport'`, one `RealReport` object per push.
`GetRealReport(Account, lng) -> bool` is the poll-based equivalent (`intMark == 1`,
`RealReportResult.RealReportList`) — replaces the legacy API's separate
`ReportQuery`/`DealQuery` pair with one unified call. `RptType` distinguishes
order/fill/etc. (`2`=期貨委託, `3`=期貨成交, ...); `OrderStatus` carries the full
status code table (0=委託成功, 8=已成交, 24=委託失效, 25=價穩失效, ...). Neither push
nor query result is parsed into a typed `Order`/`Fill` yet — Feature 06's job (needs an
idempotency-key mapping), same posture the legacy design always took.

### Account/position queries (帳務)

`GetFutStoreSummary(Account, lng) -> bool` — 期貨庫存總表查詢, a fully documented
futures position query (`FutStoreSummaryResult.FutStoreList`, fields include
`Trid`/`Commodity1`/`SettlementMonth1`/`BS1`/`Qty`/...). Replaces the legacy API's
undocumented `RA003` mechanism (`UserDefinsFunc`), which had no worked example for its
row layout. Not parsed into a typed `Position` yet — Feature 08's job.

## Python setup notes

Official 前言 > Python設定 page's Windows/Mac setup:

```python
from pythonnet import load

load("coreclr")
import clr, sys, os, pathlib

sys.path.append(str(pathlib.Path(__file__).parent.resolve()))
if sys.platform == "win32":
    os.add_dll_directory(str(pathlib.Path(__file__).parent.resolve()))
clr.AddReference("YuantaSparkAPI")  # no file extension
from YuantaOneAPI import YuantaSparkAPITrader, enumEnvironmentMode, enumLogType
```

`infrastructure/yuanta/spark_client.py` implements exactly this pattern, pointed at
`default_dll_directory()` (`C:\Yuanta\SparkAPI` by convention) instead of the script's
own folder. Requires the **.NET 8 SDK** installed system-wide (not a pip package) —
`infrastructure/yuanta/preflight.py` checks for it via `dotnet --list-runtimes`.

## Session/login (Feature 02)

`session_orchestrator.py`'s `BrokerSessionOrchestrator` is the login/session-lifecycle
implementation (`IBrokerSession`), driven by `spark_api_adapter.py`'s
`SparkApiSessionAdapter` (the real `pythonnet`-backed `SparkAdapterPort`). **Not yet
executed against a real login this session** — no .NET 8 SDK/vendor DLL/real futures
account available in this environment; written strictly from the documented API
signatures. See `docs/adr/0004-broker-session-architecture.md`'s "What's confirmed vs.
what's not verified" for the honest status — check there before assuming any of this
has been exercised against a live server.

To actually run the real (non-mock) gateway: download the SPARK API component from the
official portal, extract it to `C:\Yuanta\SparkAPI` (or point
`spark_client.default_dll_directory()` elsewhere), install the .NET 8 SDK, then set
`use_mock: false`.

## Legacy OCX API (superseded, historical reference only)

The retired COM/ActiveX pair (`YuantaOrd.ocx` trading, `YuantaQuote_v2.1.2.9.ocx`
quote) and its 32-bit-only bitness constraint, ProgID/CLSID registration, and
`AtlAxCreateControlEx` hosting mechanism are fully documented in git history (see the
pre-2026-08-19 revisions of this file and `docs/adr/0002`/`0004`) — not reproduced
here since none of it applies to SPARK API. Don't build on any of it going forward.
