# infrastructure.yuanta — vendor API inventory

Vendor deliverables live at the repo root (gitignored, proprietary, installed locally —
never committed): `交易API元件及說明文件/` (trading) and `行情API元件及說明文件/`
(quote).

Feature 02 re-extracted these facts two ways: PDF/docx text via `pypdf`/`python-docx`
(poppler wasn't available for the usual page-render extraction), and — more
importantly — **exact ProgIDs, CLSIDs, and method/event signatures read directly from
each `.ocx`'s embedded COM type library** via `comtypes.client.GetModule(ocx_path)`,
which parses the type library straight from the file and needs no COM registration.
The type library is ground truth; the PDF is not always current — see the quote OCX
callout below.

## Trading API (交易API)

COM/ActiveX OCX, MFC-based. Ships **both bitnesses**:

- `API/YuantaOrd.ocx` — 32-bit, ProgID `Yuanta.YuantaOrdCtrl.1`, CLSID
  `{70AA5E9A-4564-4C29-9403-4407FE8A7358}`, dispatch IID
  `{7AC14464-3996-4402-8CF9-1514159E157E}`, events IID
  `{90467C91-11D5-4AE4-BE11-5052E55869C8}`
- `API_x64/YuantaOrd64.ocx` — 64-bit, ProgID `Yuanta.YuantaOrdCtrl.64`, CLSID
  `{361303F7-8986-4DAC-9887-3E9FDA110FB9}`, dispatch IID
  `{CB868718-B94E-4259-991F-D1569A70433E}`, events IID
  `{32B3B217-8408-41FD-8922-B22B3DAD2474}`

Every CLSID/IID above and every signature below was confirmed against the type
library, not just the PDF — they agree for this control (unlike the quote OCX, see
below).

Core surface (from `元大BToCAPI格式.pdf`, confirmed against the type library):

- `SetFutOrdConnection(ID: BSTR, Pass: BSTR, IP: BSTR, Port: int) -> int` /
  `DoLogout() -> None`, `OnLogonS(TLinkStatus: int, AccList: BSTR, Casq: BSTR, Cast:
  BSTR)` event
- `SendOrderF(...) -> BSTR` — 15 BSTR params: `FCode`/`CommodityType`/`BranchID`/
  `AcNo`/`SubAcNo`/`OrdNo`/`BSCode`/`FutNo`/`Pri1`/`Qty1`/`OffSet`/`PriType`(M/L)/
  `OrdCond`(R=ROD,F=FOK,I=IOC)/`BSCode2`/`FutNo2` — **not called anywhere in this
  codebase**; order submission is Feature 06's job
- `OnOrdRptF` / `OnOrdMatF` events — order/match reports (fields include `Bhno`/`Acno`/
  `Suba`/`Symb`/`O_Kind`/`Buys`/`O_Prc`/`O_Qty`/`Work_Qty`/`Kill_Qty`/`Deal_Qty`/
  `Order_No`/`Oseq_No`/`Err_Code`) — not subscribed to by Feature 02 (no code
  consuming order/match pushes exists yet; that's Feature 06)
- `ReportQuery(Func, Bhno, AcNo, Suba, Stus, Kind, Cflg) -> int` +
  `OnReportQuery(RowCount: int, Results: BSTR)` — order query
- `DealQuery(Func, Bhno, AcNo, Suba, Kind) -> int` + `OnDealQuery(RowCount: int,
  Results: BSTR)` — fill query
- `UserDefinsFunc(Params: BSTR, WorkID: BSTR) -> int` +
  `OnUserDefinsFuncResult(RowCount: int, Results: BSTR, WorkID: BSTR)` — general
  query; `Func=RA003` (部位狀況查詢) is the only documented mechanism for **position
  query** — no worked example of `RA003`'s row layout exists in the PDF, so Feature 02
  only checks this call completed, it doesn't parse the row data (see ADR 0004)
- `SetLog(Enable: int)` — enables raw packet logging to a file. **Never called** by
  this codebase — the packets plausibly contain the ID/password, and enabling this
  would work against `docs/secrets-management.md`'s "never log a secret" rule.
- A parallel `RfSendOrder` / `OnRfOrdRptRF` / `OnRfOrdMatRF` / `RfReportQuery` /
  `RfDealQuery` path also exists (**國外期貨**, foreign futures) — out of scope, this
  system only trades TXF/MXF (domestic).

Endpoints: test `apitest.yuantafutures.com.tw:80`, prod
`api.yuantafutures.com.tw:80/443`.

## Quote API (行情API)

COM/ActiveX OCX `QAPI/YuantaQuote_v2.1.2.9.ocx`, ProgID `YUANTAQUOTE.YuantaQuoteCtrl.1`
(extracted from the `.ocx`'s string table — not stated anywhere in the PDF/txt), CLSID
`{8E7FB42A-1137-467E-98C6-830C9B02EA82}`, dispatch IID
`{48BD1D0C-21D8-4B72-8850-9909A7D8C205}`, events IID
`{E49166B8-9C1F-442E-A538-CD98E80CFCB1}`. **No 64-bit build shipped** — this is what
forces the whole project to be x32 (32-bit) in production (see
`docs/adr/0001-python-version-and-runtime.md` and
`docs/adr/0002-ui-framework-and-com-hosting.md`). Requires a separate market-data
agreement (via 營業員) beyond plain trading API access.

**The live type library disagrees with `元大行情API.pdf` in several places** — the
shipped build is evidently newer/extended relative to what the PDF documents. Confirmed
from the type library (authoritative — see `vendor_codes.py`):

- `SetMktLogon(user: BSTR, pass: BSTR, ip: BSTR, port: BSTR, ReqType: int, SetMap:
  int) -> None` — PDF only shows 4 params; `ReqType`/`SetMap` are undocumented
  anywhere available to this session.
- `AddMktReg(symbol: BSTR, updmode: BSTR, ReqType: int, SetMap: int) -> int` — PDF's
  `UpdateMode` (1=Snapshot, 2=Update, 4=SnapshotUpd) is real, but the live
  dispinterface marshals it as a **string**, not an int, plus the same two extra
  params.
- `DelMktReg(symbol: BSTR, ReqType: int) -> int`
- `OnMktStatusChange(Status: int, Msg: BSTR, ReqType: int)` — `Msg[0]` code table is
  unchanged from the PDF (see `vendor_codes.QUOTE_MSG_CODE_MESSAGES`), just with a
  trailing `ReqType` added to the event.
- `OnRegError(symbol: BSTR, updmode: int, ErrCode: int, ReqType: int)`
- `OnGetMktAll(...)` — same 19 fields as the PDF (`Symbol`/`RefPri`/.../`FDSQty`) plus
  a trailing `ReqType`.
- A whole undocumented "Tick" subsystem also exists in the live control
  (`AddTickReg`/`DelTickReg`/`GetTickRangeData`/`OnGetTickData`/etc.) — not in the PDF
  at all, not used by this codebase.

`ReqType`/`SetMap` have no documented meaning found anywhere; `infrastructure/yuanta/
quote_ocx_adapter.py` passes `0` for both as an explicitly-flagged placeholder — see
`vendor_codes.py` and ADR 0004's "What's not verified".

`RegErrCode` (`AddMktReg`/`DelMktReg`'s return): `0`=Success, `1`=Symbol_err (length
<4 or >13), `2`=Mode_err, `3`=Connect_err. Separate async-only `OnRegError`
`ErrorCode`: `1`=UserNotLogin, `2`=StkNotExist, `3`=ContractBreak (`OnRegError` "成功則
不通知" — only fires on failure).

Endpoints: domain `apiquote.yuantafutures.com.tw`, T session port 80/443, T+1 session
port 82/442 (Feature 02 only connects the T session).

## Python-specific note

`交易API元件及說明文件/元大API交易PYTHON注意事項.docx` (vendor's own Python guidance,
text extracted this session via zip/xml parsing since it has no readable Python sample
code, just a note) confirms:

- Vendor's sample was built on **Python 3.9, wxPython 4.1.1, comtypes 1.1.11**.
- Bitness of the OCX and the Python interpreter **must match** — no cross-bitness COM
  marshaling is supported.
- To use the 32-bit OCX, the sample's `AtlAxCreateControlEx` ProgID string must be
  changed from `"Yuanta.YuantaOrdCtrl.64"` to `"Yuanta.YuantaOrdCtrl.1"`.

This codebase deliberately targets **Python 3.11** instead of the vendor's 3.9 (EOL
Oct 2025) — see `docs/adr/0001-python-version-and-runtime.md`. `ocx_host.py` hosts
both controls via `AtlAxCreateControlEx` exactly as this note describes, but wraps the
returned control pointer using bindings generated at runtime from the `.ocx`'s type
library (`comtypes.client.GetModule`) rather than hand-written ones — see ADR 0004.

## Session/login (Feature 02)

`session_orchestrator.py`'s `BrokerSessionOrchestrator` is the login/session-lifecycle
implementation (`IBrokerSession`). **Verified working end-to-end** against the real
vendor `.ocx` files this session (2026-08-16): activation, per-user COM registration
(no admin rights — `com_registration.py`), real dispatch-method invocation, and
event-sink delivery all confirmed via a live network round-trip to the vendor's test
endpoint (`SetFutOrdConnection` → real `OnLogonS` with a real `TLinkStatus`). See
`docs/adr/0004-broker-session-architecture.md` for the full design and its "Execution
attempt findings"/"What's confirmed working"/"What's still not verified" sections for
the complete, honest trail — including three real bugs found and fixed along the way
(wrong `AtlAxCreateControlEx` argument form, wrong DLL search path, and a discarded
COM event-connection reference that silently killed event delivery).

To actually run the real (non-mock) gateway locally: copy the vendor's component
folders to `C:\Yuanta\API` and `C:\Yuanta\QAPI` (the vendor's own documented layout —
see `使用說明.txt` in each package), then set `use_mock: false` — `composition.py`
handles COM registration automatically. `tests/infrastructure/
test_yuanta_ocx_activation.py` (opt-in, `TFX_QUANT_OCX_ACTIVATION_TEST=1`) exercises
this same path as an automated regression check whenever the vendor files are present.
