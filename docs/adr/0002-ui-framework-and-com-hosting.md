# ADR 0002 — UI framework and vendor API hosting

## Status

Accepted (Feature 01). Revised (Feature 02) to reflect `AtlAxCreateControlEx` instead
of `wx.lib.activex.ActiveXCtrl` — see git history. **Superseded (2026-08-19, SPARK API
pivot)**: the legacy COM/ActiveX OCX hosting problem this ADR was written to solve no
longer exists. The client's implementation prompts now mandate the official **元大
SPARK API** as the only valid spec source (see ADR 0001's 2026-08-19 revision) — a
.NET 8 component accessed via `pythonnet` CLR interop, not a windowed ActiveX control.
This revision replaces the hosting decision; the wxPython choice for the UI itself is
unchanged.

## Context

The old vendor deliverable was a pair of MFC-based ActiveX/OCX controls that had to be
activated inside a real window (`AtlAxCreateControlEx`) and driven via COM
dispatch/event-sink plumbing (`comtypes`) — hence this ADR's original "which thread
hosts the OCX" question.

SPARK API has no equivalent concept. Per the official docs' own Python usage example
(基礎 → 登入 page), the whole integration is:

```python
from pythonnet import load

load("coreclr")
import clr

clr.AddReference("YuantaSparkAPI")
from YuantaOneAPI import YuantaSparkAPITrader, enumLogType, enumEnvironmentMode

objYuantaSparkAPI = YuantaSparkAPITrader()
objYuantaSparkAPI.OnResponse += on_response  # ordinary .NET event, += subscribes
objYuantaSparkAPI.Open(enumEnvironmentMode.UAT)
objYuantaSparkAPI.Login(account, password)  # Windows: 2 args, no cert path
```

`YuantaSparkAPITrader` is a plain .NET object — no window handle, no message pump, no
COM registration step. All async results (login, orders, quotes, reports, queries)
arrive through **one unified event**, `OnResponse(intMark, dwIndex, strIndex,
objHandle, objValue)`, dispatched by matching `strIndex` against the function name that
produced the result (e.g. `strIndex == 'Login'`). This replaces the old per-function
COM event surface (`OnLogonS`/`OnOrdRptF`/`OnGetMktAll`/...) with one dispatch point.

## Decision

- **wxPython stays** for all desktop UI — this choice was never about COM hosting, it
  was about the framework itself, and nothing about the vendor pivot changes that.
- **Delete the entire OCX-hosting subsystem**: `infrastructure/yuanta/ocx_host.py` and
  `infrastructure/yuanta/com_registration.py` are removed outright, not adapted — there
  is no window to host, no ProgID/CLSID to register, no `AtlAxCreateControlEx` call.
  `vendor_codes.py` (the old OCX's undocumented `ReqType`/`SetMap` findings) is deleted
  too — none of it applies to SPARK API.
- **`pythonnet` replaces `comtypes`** for vendor interop (see ADR 0001). One
  `YuantaSparkAPITrader` instance replaces the old separate trade/quote OCX controls —
  a single session object now serves login, market data, trading, reports, and account
  queries.
- **`OnResponse` is the single event subscribed via `+=`** (a genuine .NET event
  add-handler, distinct from `comtypes`' COM connection-point `GetEvents()` dance —
  no risk of the old "discarded event-connection reference silently kills delivery"
  bug class, since there's no separate connection-point object to keep alive).
  `intMark` distinguishes system vs. query/push responses; `strIndex` distinguishes
  which function/report the payload belongs to. The adapter layer's job is entirely
  about **dispatching one Python callback by `strIndex`**, not managing multiple COM
  event sinks.
- **Threading**: `.NET` events fire on a CLR-managed thread, not the wx main thread —
  same fundamental shape as the old COM callback threading concern, so ADR 0003's
  `EventCoordinator` design (convert to an internal `Event` immediately, hand off to the
  queue, never touch wx widgets directly from the callback thread) is **reused
  unchanged**. Only what feeds the coordinator changes (one `OnResponse` handler instead
  of several COM event-sink methods).
- Feature 01's "no order-sending UI" constraint is untouched by this pivot —
  `readiness_frame.py` still has no code path that can call `SendFutureOrder` or
  equivalent; that remains Feature 06's job.

## Consequences

- `pyproject.toml`: `comtypes` dependency removed; `pythonnet` added (see ADR 0001).
  `[[tool.mypy.overrides]]` for `module = "comtypes.*"` removed; a `pythonnet`/`clr`
  override likely needed instead (the `clr` module is dynamically created by
  `pythonnet` at import time and has no static stubs).
- `infrastructure/yuanta/trade_ocx_adapter.py` and `quote_ocx_adapter.py` are replaced
  by a single adapter module wrapping `YuantaSparkAPITrader` and its `OnResponse`
  dispatch — see the rewritten ADR 0004 for the concrete design.
- No DLL-search-path/MFC/COM-registration workarounds are needed — SPARK API's own
  setup note says the Python component just needs `sys.path`/`os.add_dll_directory`
  pointed at the folder containing `YuantaSparkAPI.dll` (and its sibling `.dll` files)
  before `clr.AddReference`, which is a normal file-path concern, not a Windows COM
  subsystem one.
