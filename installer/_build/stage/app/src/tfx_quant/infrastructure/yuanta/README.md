# infrastructure.yuanta — vendor API inventory

This codebase integrates the **legacy Yuanta COM/ActiveX OCX pair** (trade + quote).
An earlier revision pivoted to the newer "SPARK" .NET API (`YuantaSparkAPI.dll` via
`pythonnet`); **that pivot was reverted** — there is no SPARK code in `src/`, and
`desktop/composition.py` wires the OCX broker exclusively.

**API contract source of truth** (both gitignored, installed locally):

- Trade: `交易API元件及說明文件/` — `API/` (32-bit), `API_x64/` (64-bit),
  `使用說明.txt`, `元大BToCAPI格式.pdf`, `元大API交易PYTHON注意事項.docx`.
- Quote: `行情API元件及說明文件/` — `QAPI/`, `使用說明.txt`, `元大行情API.pdf`,
  `YuantaQuoteAPI Sample.py`.

Do not infer API behaviour from the SPARK site, old SPARK SDK, or memory — only what
those folders document. Anything not documented there is a blocker, isolated in the
adapter, never invented.

## Bitness

**32-bit only.** The quote OCX has no 64-bit build; `quote_com_host.py` hard-refuses
a non-32-bit interpreter. The trade OCX exists in both bitnesses but is loaded at the
interpreter's bitness. `preflight.py` / `quote_preflight.py` check this.

## Trade API — `YuantaOrd` ActiveX (`infrastructure/yuanta/legacy_ocx_host.py`)

| | |
|---|---|
| Component folder | `C:\Yuanta\API` (32-bit) — override with `TFX_QUANT_YUANTA_API_DIR` |
| OCX | `YuantaOrd.ocx` (`YuantaOrd64.ocx` for 64-bit) |
| Dependent DLLs | `YuantaOrdLib.dll` (`YuantaOrdLibX64.dll`), `YuantaCAPIDLL.dll` |
| ProgID | `Yuanta.YuantaOrdCtrl.1` (`Yuanta.YuantaOrdCtrl.64`) |
| Registration | `install_YTFutOrdAP.bat` as Administrator; do not move files afterward |
| VC++ runtime | `vcredist_x86.exe` if the MFC/CRT runtime is missing |
| Endpoints | test `apitest.yuantafutures.com.tw:80`, prod `api.yuantafutures.com.tw:80/443` |
| Hosting | `AtlAxCreateControlEx` against a real wx window handle on the UI thread (not windowless `CreateObject`), via `ocx_hosting.create_activex_control` + `preload_control` |
| Events | `OnLogonS`, `OnOrdResult`, `OnOrdRptF`, `OnOrdMatF`, `OnReportQuery`, `OnDealQuery`, `OnUserDefinsFuncResult` |
| Account format | futures account, `F` prefix |
| `OnLogonS` `TLinkStatus` | `-1` = lsLinkBroken, `4` = lsCAError (certificate), `5` = lsPassError (password) |

Adapters: `legacy_broker.py` (`IBrokerSession` + `TradeGatewayPort`),
`legacy_order_api.py`, `event_publisher.py`. Mock equivalents:
`mock_broker_session.py`, `mock_trade_gateway.py`.

Yuanta's trade test host is retired — `environment: TEST` uses the local mock, not a
UAT login.

## Quote API — `YuantaQuote` ActiveX (`infrastructure/yuanta/quote_com_host.py`)

| | |
|---|---|
| Component folder | `C:\Yuanta\QAPI` — override with `TFX_QUANT_YUANTA_QAPI_DIR` |
| OCX | `YuantaQuote_v2.1.2.9.ocx` |
| ProgID | `YUANTAQUOTE.YuantaQuoteCtrl.1` |
| Registration | `install_ytocx.bat` as Administrator |
| Access | requires a separate "行情 API" application on top of the trade API |
| Endpoint | `apiquote.yuantafutures.com.tw`; T-line port 80/443, T+1-line port 82/442 |
| Connect | `SetMktLogon(User, pass, IP, PORT, ReqType, SetMap)` — `PORT` as a string, `SetMap=0`; `ReqType=1` = T-line, `ReqType=2` = T+1-line |
| Register symbol | `AddMktReg(Symbol, UpdMode, ReqType, SetMap)` — `UpdMode` `"1"`/`"2"`/`"4"` as strings; `SnapshotUpd (4)` for current + updates |
| Unregister | `DelMktReg(Symbol, ReqType)` |
| Events (sink) | `OnMktStatusChange(Status, Msg, ReqType)`, `OnRegError(Symbol, UpdMode, ErrorCode, ReqType)`, `OnGetMktAll(...)` — no leading COM `this` (comtypes strips it via the explicit `_DYuantaQuoteEvents` interface) |
| History | **not available** — the OCX only pushes live quotes. All 60-minute bars are aggregated from live quotes actually received after login, persisted first; disconnection gaps are preserved, never backfilled. |

Adapters: `quote_adapter.py` (`YuantaQuoteAdapter`), `quote_com_host.py`
(`YuantaQuoteComHost`). The quote runtime lives at `desktop/quote_runtime.py`.
`YuantaQuoteAPI Sample.py`'s sample used Python 3.9 / wxPython 4.1.1 / comtypes
1.1.11; a live session is confirmed on 32-bit Python 3.11 / comtypes 1.4.16 (the
pinned requirement that matters is the 32-bit interpreter).

## Credentials (`infrastructure/yuanta/credentials.py`)

Never in `TradingSettings` or any JSON file. Entered in `desktop/login_dialog.py`
each session; the password is stored (opt-in only, after a successful login) in
Windows Credential Manager via `keyring` (DPAPI). Keyring service names:

- `tfx_quant.yuanta` — trade login password
- `tfx_quant.yuanta.quote` — quote login password (TEST env)
- key `certificate-import-password` under `tfx_quant.yuanta` — the PFX password

Certificate import: `ensure_certificate_imported` shells out to
`certutil -importpfx -user` (per-user, no admin), password piped via stdin so it
never appears in the process argument list.

## Python setup notes

- 32-bit Python 3.11, `comtypes`, `wxPython`. `comtypes.client.GetModule(<ocx>)`
  generates the typed wrappers under `comtypes.gen` at runtime.
- The OCX's own directory must be on the DLL search path — `preload_control` does
  this with `LoadLibraryExW(..., LOAD_WITH_ALTERED_SEARCH_PATH)`.
- CI installs no OCX and needs none: the real adapters only touch the vendor
  components at runtime, and every test uses the mock/fake gateways.
