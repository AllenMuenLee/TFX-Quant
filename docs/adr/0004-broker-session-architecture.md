# ADR 0004 — Broker session architecture (Feature 02)

## Status

Accepted (Feature 02). **Updated 2026-08-16, twice.** First with real execution
findings after installing a 32-bit (x32) Python interpreter and hitting a legacy-DLL
loading failure; the same session then **fully resolved that failure** and confirmed
real, end-to-end activation, method invocation, and event delivery against the actual
vendor OCX files with a live network round-trip. See "Execution attempt findings" and
"What's confirmed working" below — the earlier "unresolved legacy MFC compatibility
issue" framing is superseded; keep reading past step 6 for the actual fix. Also: this
repo now says **x32** for 32-bit throughout (not "x86"), and the whole project — not
just `infrastructure.yuanta`/`desktop` — is x32-only; see ADR 0001's Feature 02
revision for why.

## Context

Feature 01 wired only mock Yuanta gateways and left the real login/session
integration as Feature 02's job, flagging two open questions in ADR 0002: whether the
OCX controls live on the wx main UI thread or a separate STA thread, and how the
"dedicated dispatcher" the implementation prompt asks for should work. This session
re-extracted the vendor deliverables (PDFs via `pypdf`/`python-docx`, since poppler
wasn't available for page rendering; ProgIDs/CLSIDs/exact method signatures directly
from each `.ocx`'s embedded type library via `comtypes.client.GetModule()`, which
reads the type library from the file itself — no COM registration required) to ground
every decision below in verified facts rather than the PDFs alone.

**Important finding**: the shipped quote OCX (`YuantaQuote_v2.1.2.9.ocx`)'s live type
library disagrees with `元大行情API.pdf` in several places — `SetMktLogon`/
`AddMktReg`/`DelMktReg` all carry extra `ReqType`/`SetMap` parameters the PDF doesn't
mention, and `AddMktReg`'s `updmode` is marshaled as a string, not the int the PDF
implies. The PDF is evidently stale relative to this build. See
`infrastructure/yuanta/README.md` and `infrastructure/yuanta/vendor_codes.py` for the
full comparison — the real adapter code is written against the type library, not the
PDF, wherever they conflict.

## Decisions

### 1. OCX hosting: wx main UI thread, via `AtlAxCreateControlEx`

Both OCXs are **windowed** ActiveX controls (MFC `COleControl`-derived). The vendor's
own Python compatibility note names `AtlAxCreateControlEx` (an ATL Win32 API) as the
embedding mechanism — not `wx.lib.activex.ActiveXCtrl`, which ADR 0002 had tentatively
assumed. MFC OLE controls generally only create their internal window (and therefore
only pump whatever network I/O their message handling depends on) once in-place
activated inside a container window; a bare `comtypes.client.CreateObject()`
(`CoCreateInstance` with no in-place activation) does work for calling the *trading*
control's automation methods (confirmed this session), but the *quote* control raised
`E_UNEXPECTED` ("Catastrophic failure") on `SetMktLogon` without a window and worked
once hosted in one — so `infrastructure/yuanta/ocx_host.py` hosts both uniformly
inside a hidden `wx.Frame`'s `HWND` via `AtlAxCreateControlEx` rather than relying on
a control-specific exception to the rule.

This resolves ADR 0002's open question: the control lives on **whatever thread
created that `wx.Frame`**, which in this app is always the wx main UI thread, since
that's the only thread pumping Windows messages.

**Three mechanical corrections, all confirmed this session** (see "Execution attempt
findings" — the first draft of `ocx_host.py` got each of these wrong and none of it
worked until they were fixed):
- `AtlAxCreateControlEx`'s `lpszName` must be the control's **registered ProgID**, not
  a raw file path — a bare path silently activates the system `IWebBrowser2` control
  instead (ATL treats an unrecognized string as "navigate here"), which still
  "succeeds" (`S_OK`) but is a completely different, wrong object.
- Registration is required, but **not admin rights** — see decision 7
  (`com_registration.py`).
- The OCX's own CRT/MFC dependency DLLs must be resolved via
  `LOAD_WITH_ALTERED_SEARCH_PATH` before activation — see decision 7.

### 2. The "dedicated dispatcher" is `EventCoordinator`, not a new thread

The implementation prompt requires a dispatcher that guarantees OCX callbacks don't
block and that callback data is copied to an internal DTO immediately. Every event
sink method in `trade_ocx_adapter.py`/`quote_ocx_adapter.py` does the minimum
possible — parse the primitive callback args and call straight into
`BrokerSessionOrchestrator`, which itself does nothing beyond updating in-memory state
and calling `EventCoordinator.publish()` (already thread-safe, callable from any
thread per ADR 0003). All real handling happens later, on the coordinator's own
consumer thread. No second OCX-hosting thread is introduced — it would need to
reimplement a hidden-window message pump for no benefit, since the callback hand-off
is already non-blocking via the existing coordinator.

### 3. `IBrokerSession` is additive, not a replacement

`application/ports/broker_session.py`'s `IBrokerSession` is a new, richer
login/lifecycle/capability port. Feature 01's `TradeGatewayPort`/`QuoteGatewayPort`
(narrow query/subscribe surface used by `StartupSafetyGate`) are unchanged —
`ServiceContainer` gains a new `broker_session: IBrokerSession` field alongside them.
For the real (non-mock) branch, `infrastructure/yuanta/
broker_session_gateway_views.py` provides thin `TradeGatewayPort`/`QuoteGatewayPort`
views over the real session that delegate the boolean readiness checks
(`is_logged_in`, `is_market_data_valid`) but **raise `NotImplementedError`** for
`query_open_orders`/`query_positions`/`subscribe` rather than fabricating an "always
empty" answer — see decision 5.

### 4. Split real vs. testable logic

`BrokerSessionOrchestrator` (`infrastructure/yuanta/session_orchestrator.py`) is pure
Python — no comtypes, no wx — driven by thin `TradeAdapterPort`/`QuoteAdapterPort`
protocols it calls out to, and `handle_*` callback methods a real adapter or test
double calls. This is what makes login sequencing (登入 → 委託查詢 → 成交查詢 → 持倉查詢
→ 行情訂閱 → session-ready), capped-backoff retry, capability tracking, and
duplicate/out-of-order callback guarding fully unit-testable without any real OCX.

Duplicate/out-of-order safety comes from two combined checks on every `handle_*` call:
the orchestrator only accepts a callback while it's in the exact phase expecting it,
**and** the callback's `generation` (bumped on every `start()`/retry attempt) must
match the orchestrator's current generation. A real adapter creates a fresh event sink
closed over the generation active when it issued the corresponding request, so a stale
callback from a superseded attempt is provably distinguishable from a fresh one.

### 5. Query results are not parsed into typed domain objects yet

`ReportQuery`/`DealQuery` (order/fill query) have PDF-documented worked examples with
exact field names — but building a real `Order`/`Fill` domain object from them would
need an idempotency key (`ClientOrderId`) the broker's raw response doesn't carry;
that identity mapping is Feature 06's job (order/fill state machine), not Feature 02's.
`UserDefinsFunc`'s `RA003` (position query) has **no worked example at all** in the
PDF, so its row layout would have to be guessed — left for Feature 08 (position
reconciliation) to confirm. `BrokerSessionOrchestrator` only tracks whether each query
*completed*, for sequencing/capability purposes; the raw result strings aren't parsed
further. This is also why `BrokerSessionQuoteGatewayView.subscribe()` raises rather
than translating an `Instrument`/`ContractMonth` into a vendor symbol code — that
encoding (`EasyWin` format) isn't documented in what this session has and is Feature
03's job.

### 6. Credentials and account resolution

Per the already-existing rule in `docs/secrets-management.md` (env var or Windows
Credential Manager, by name, never in `TradingSettings`/JSON): the 元大歸戶 ID comes
from `TFX_QUANT_YUANTA_USER_ID`, the password from `keyring.get_password
("tfx_quant.yuanta", user_id)` (Windows Credential Manager, DPAPI-backed). A session's
target account is resolved by: exactly one futures account returned by login →
auto-selected; otherwise the optional env var `TFX_QUANT_YUANTA_ACCOUNT_NO` (same
OS-level pattern, not a `TradingSettings` field) can disambiguate; otherwise
`select_account()` must be called explicitly (the `ReadinessFrame` account picker) —
with no unique account resolved, `BrokerSessionReady` never fires, so the strategy can
never start, matching "找不到唯一目標帳號，不得啟動策略".

### 7. Per-user COM registration, no admin rights required

The vendor's own install scripts (`install_*.bat`) call `regsvr32`, which writes to
`HKEY_LOCAL_MACHINE\Software\Classes` and genuinely requires administrator rights —
confirmed this session: it fails with `SELFREG_E_CLASS` (`0x80040200`) without them.
Requiring every operator to have (or be granted) admin rights just to run the desktop
app is a real deployment obstacle, so `infrastructure/yuanta/com_registration.py`
instead writes the same registry values under `HKEY_CURRENT_USER\Software\Classes` —
a standard, documented, non-admin COM registration location that Windows merges into
every `HKEY_CLASSES_ROOT` lookup. `desktop/composition.py`'s real branch (via
`preflight.run_preflight_checks()`) calls `register_all_per_user()` (idempotent,
best-effort) before checking registration status, so the operator's only remaining
manual step is copying the vendor's component folders to the conventional install
paths (`C:\Yuanta\API`, `C:\Yuanta\QAPI` — the vendor's own documented layout) — no
`regsvr32`, no UAC prompt.

Separately, `OcxHost` pre-loads the target `.ocx` with
`LoadLibraryExW(..., LOAD_WITH_ALTERED_SEARCH_PATH)` before activating it: the OCX's
own CRT/MFC dependency DLLs (`msvcr90.dll` etc.) live alongside the `.ocx` file, not
alongside `python.exe`, and Windows' default `LoadLibrary` search order only checks
the directory of the process's main EXE — not the directory of a DLL being loaded by
full path — so those dependencies otherwise fail to resolve. This is what the
"Execution attempt findings" below originally misdiagnosed as a fundamental CRT
compatibility problem; it wasn't.

## Execution attempt findings (2026-08-16)

At the user's request, this session tried to actually run the real adapters, not just
write them. Steps taken, in order:

1. Installed a 32-bit Python 3.11.9 interpreter (`winget install --id Python.Python.3.11
   --architecture x86`, no admin required) and rebuilt the project's **sole** venv from
   it (see ADR 0001). Every dependency (`comtypes` 1.4.16, `wxPython` 4.3.1, `pydantic`
   2.13.4, `keyring`) imports cleanly under it; the full test suite and `ruff`/`mypy`/
   `lint-imports` all pass under this x32 venv (168 passed, 1 opt-in-skipped).
2. Attempted `regsvr32` on `API/YuantaOrd.ocx` (32-bit trading OCX) without admin
   rights (this account is not a machine administrator) — failed silently (exit code
   3, no usable diagnostic).
3. Diagnosed directly via `ctypes.windll.kernel32.LoadLibraryW` instead of `regsvr32`:
   loading `YuantaOrd.ocx` itself fails with `WinError 126` ("module not found" — an
   unresolved dependency, not a missing file). Loading its dependencies individually
   narrowed it down: `msvcr90.dll` (VC++ 2008 CRT) loads but its `DllMain` fails with
   **`WinError 1114`** ("A dynamic link library (DLL) initialization routine failed");
   `msvcp90.dll`/`mfc90.dll`/`mfc90u.dll` all fail with 126 because of that upstream
   failure.
4. Installed `Microsoft.VCRedist.2008.x86` and `Microsoft.VCRedist.2010.x86` via
   winget (both succeeded without admin). Inspected `C:\Windows\WinSxS\Manifests` for
   `Microsoft.VC90.CRT`/`Microsoft.VC90.MFC` assemblies: the CRT assembly (and its
   version-redirect policy) exists in WinSxS (version 9.0.30729.9635 — newer than what
   the vendor bundled, 9.0.21022.8), but **no `Microsoft.VC90.MFC` assembly exists in
   WinSxS at all** — the standard `vcredist_x86.exe` does not install MFC; historically
   that required a separate, no-longer-distributed hotfix (KB2538242-class update).
5. Copied the newer WinSxS `msvcr90.dll`/`msvcp90.dll` into a scratch folder alongside
   the vendor's `mfc90.dll`/`mfcm90.dll` and manifests, to test whether a newer CRT
   would resolve the `DllMain` failure via private/local assembly deployment (Windows
   prefers a private copy in the same directory as the loading module over WinSxS).
   **It made no difference** — `msvcr90.dll` still fails with `WinError 1114` even at
   the newer version, so the failure is not simply "wrong/old CRT version"; it's some
   other DllMain-time incompatibility with this Windows build.
6. Set up (but hadn't yet retried when the session was paused for an unrelated
   clarifying question) a Windows Application Compatibility layer override
   (`HKCU\...\AppCompatFlags\Layers` = `WIN7RTM` for the x32 `python.exe`). **This
   turned out to be a dead end** — retried later (step 9) and made no difference; it
   was removed again. The real fix was steps 7–9 below.
7. Root-caused properly: loading `YuantaOrd.ocx` directly with plain `LoadLibraryW`
   gives `WinError 126` because the loader can't find `msvcr90.dll` at all — **not**
   because `msvcr90.dll` is broken. `LoadLibraryW`'s default search order checks the
   directory of the *process's* main EXE (`python.exe`'s directory), not the directory
   of the target DLL being loaded by full path — so the OCX's sibling dependency DLLs,
   sitting right next to it, were never found by a plain load. Confirmed by extracting
   the OCX's own embedded manifest resource (`LoadLibraryEx(..., 
   LOAD_LIBRARY_AS_DATAFILE)` + `FindResource(RT_MANIFEST)`): it declares no
   `Microsoft.VC90.CRT`/`MFC` dependency at all, so there's no SxS/WinSxS activation
   context involved — it's a plain, ordinary DLL search-path problem. The earlier
   `WinError 1114` on a directly-loaded standalone `msvcr90.dll` (step 3) was a red
   herring caused by testing it in isolation the wrong way, not a real incompatibility
   — confirmed by reproducing the *exact same* 1114 from a completely different host
   process (32-bit `powershell.exe`, `Is64BitProcess=False` verified), which only
   proved the standalone-load path was flawed, not that the DLL itself was broken.
   **Fix**: `LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH)` — makes the
   loader search the target file's own directory, exactly like a normal EXE launched
   from that folder would. This alone made `YuantaOrd.ocx` load successfully.
8. With loading fixed, `AtlAxCreateControlEx(raw_file_path, ...)` still returned
   `S_OK` but activated the wrong thing — `IWebBrowser2` (Internet Explorer's
   WebBrowser control), confirmed via `IDispatch::GetTypeInfo`: the returned object's
   members were `Navigate2`/`QueryStatusWB`/`ExecWB`/etc., not
   `SetFutOrdConnection`/`DoLogout`/etc. ATL evidently treats an unrecognized raw path
   as "navigate here" and falls back to hosting the system browser control instead of
   raising an error. The vendor's own Python note only ever shows ProgID strings for
   this parameter — never a raw path — which was the correct signal from the start.
9. Registering the ProgID via `regsvr32`/`DllRegisterServer` (calling it directly via
   `GetProcAddress`, bypassing `regsvr32.exe` itself) returned `SELFREG_E_CLASS`
   (`0x80040200`) — this account has no administrator rights, and MFC's
   `COleObjectFactory::UpdateRegistry` writes directly to
   `HKEY_LOCAL_MACHINE\Software\Classes`, which requires them. **Fix**: manually wrote
   the same registry values (`CLSID\{...}`, `\ProgID`, `\InprocServer32` +
   `ThreadingModel=Apartment`, `\Control`, `<ProgID>\CLSID`) directly under
   `HKEY_CURRENT_USER\Software\Classes` instead — a standard, documented, no-admin-
   required COM registration location Windows merges into every
   `HKEY_CLASSES_ROOT` lookup. Confirmed via registry readback through the merged
   `HKEY_CLASSES_ROOT` view. With this in place, `AtlAxCreateControlEx(progid, ...)`
   correctly activates the *real* Yuanta control (type info GUID matched
   `_DYuantaOrd`'s IID exactly, member list `SetFutOrdConnection`/`DoLogout`/
   `SendOrderF`/`ReportQuery`/`DealQuery`/...).
10. Called `SetFutOrdConnection("TESTUSER", "TESTPASS", "apitest.yuantafutures.com.tw",
    80)` on the correctly-activated control, advised an event sink via
    `comtypes.client.GetEvents`, and pumped Win32 messages: **`OnLogonS` fired for
    real**, with `TLinkStatus=-1` (`lsLinkBroken`) — a real network round-trip to the
    vendor's actual test server, a real (if placeholder-credential) failure response,
    not a crash or timeout.
11. Wiring this into the actual production adapter code (not the scratch probe)
    initially received *no* event at all, with no exception either. Root cause: the
    first draft of `OcxHost.advise()` called `comtypes.client.GetEvents(...)` without
    storing its return value — the returned `_AdviseConnection` object is what keeps
    the COM connection-point subscription alive; discarding it lets Python garbage-
    collect it immediately, silently un-advising the connection before any event could
    arrive, without raising anything. **Fix**: store it as an `OcxHost` instance
    attribute for the host's whole lifetime. This class of bug (silent, no exception,
    "just doesn't fire") is exactly the kind of thing that's easy to miss without
    actually executing the code against a real server — which is why this whole
    verification pass mattered.
12. Repeated steps 7–11 for the quote OCX (`YuantaQuote_v2.1.2.9.ocx`,
    `YUANTAQUOTE.YuantaQuoteCtrl.1`): activation and `SetMktLogon` invocation succeed
    the same way, but **only when hosted in a real window** — calling it via headless
    `comtypes.client.CreateObject` (no window) raised `E_UNEXPECTED` ("Catastrophic
    failure"), while the identical call succeeded once hosted via
    `AtlAxCreateControlEx` in a `wx.Frame`, confirming decision 1's "host both
    uniformly" choice was the right call. `OnMktStatusChange` was not observed firing
    within 20s using placeholder credentials, unlike the trading OCX's prompt
    response — unconfirmed whether that's because the quote API needs a genuine
    market-data agreement to respond at all (see `infrastructure/yuanta/README.md`) or
    an unresolved gap; not chased further.

## What's confirmed working (2026-08-16)

Real, end-to-end, against the actual vendor `.ocx` files, through the actual
production code (`OcxHost`, `com_registration.py`, `YuantaTradeOcxAdapter`,
`YuantaQuoteOcxAdapter` — not just scratch scripts), with a real network round-trip:

- The x32 interpreter, venv, and every Python dependency this adapter code needs
  (`comtypes` 1.4.16, `wxPython` 4.3.1 — the ADR 0002 wheel-availability risk was
  unfounded).
- Per-user COM registration (`com_registration.register_all_per_user()`) with no
  admin rights, verified via registry readback and via successful activation.
- `OcxHost` activating the *correct* real control (not a WebBrowser fallback) for
  both the trading and quote OCX, via `AtlAxCreateControlEx` + the registered ProgID.
- Calling a real dispatch method (`SetFutOrdConnection`, `SetMktLogon`) on the
  activated control.
- Event-sink delivery: `OnLogonS` firing with a real `TLinkStatus` from a real
  network round-trip to `apitest.yuantafutures.com.tw`, received by the actual
  `BrokerSessionOrchestrator`-facing adapter code via a Win32 message pump.
- Regression coverage: `tests/infrastructure/test_yuanta_ocx_activation.py` (opt-in,
  `TFX_QUANT_OCX_ACTIVATION_TEST=1`, auto-skipped wherever the vendor files or an x32
  interpreter aren't present) exercises exactly this path through the real adapters
  on every run where it's enabled — it isn't a one-off finding, it's now a repeatable
  check.

## What's still not verified

- The real meaning of the quote OCX's undocumented `ReqType`/`SetMap` parameters
  (still passing `0`/`0` — see `vendor_codes.py`).
- Whether the quote OCX would respond with a real `OnMktStatusChange` given a
  genuinely-provisioned market-data account (untestable without one — see step 12).
- Whether a passive mid-session disconnect on the *trading* OCX surfaces through any
  event at all (no such event was found in the PDF — `handle_trade_disconnected()` is
  a best-effort integration seam, not a confirmed callback wiring).
- A full login with *real* Yuanta credentials reaching `BrokerSessionReady` (needs
  real customer credentials — see `tests/infrastructure/test_yuanta_live_smoke.py`,
  which is opt-in for exactly this reason and wasn't run this session).
- `ReportQuery`/`DealQuery`/`UserDefinsFunc` and their events, and `AddMktReg`/
  `DelMktReg`/`OnRegError` — not separately exercised (they need a real logged-in
  session), though they use the same now-proven activation/invocation/event-sink
  mechanics as `SetFutOrdConnection`/`OnLogonS`.

`BrokerSessionOrchestrator`'s sequencing, retry, capability-tracking, and
callback-guarding logic — the part that doesn't touch COM/wx at all — has always had
full unit-test coverage (`tests/infrastructure/test_broker_session_orchestrator.py`),
independent of everything above.
