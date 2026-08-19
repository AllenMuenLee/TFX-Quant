# ADR 0001 — Python version and runtime

## Status

Accepted (Feature 01). **Revised (Feature 02, 2026-08-16)**: widened the x32 scope to
the whole system (see git history for that revision's text). **Superseded in part
(2026-08-19, SPARK API pivot)**: the client rewrote every implementation-prompt file to
mandate that the *only* valid API specification source going forward is the official
線上文件 for **元大 SPARK API**
(https://www.yuanta.com.tw/file-repository/content/API/page/index.html and its docs
site), not the legacy COM/ActiveX vendor packages this ADR originally described. SPARK
API is a completely different product — cross-platform, both bitnesses — so the
system-wide **x32 hard constraint no longer applies**. This revision replaces the
bitness decision; the Python-version reasoning is materially unchanged.

## Context

`implementation prompt/README.md` mandates Python for the entire system and a version
"仍在安全維護期且與元大 API 相容" (still in its security maintenance window and
compatible with the Yuanta API).

The vendor's legacy COM/ActiveX trading+quote OCX pair — what earlier revisions of this
ADR were written against — is now confirmed **legacy, maintenance-only**
(`YuantaOneAPI_Com.zip` on the official portal, listed alongside Delphi/WPF as
"legacy"). The actively-developed product is **元大 SPARK API**
(`YuantaSparkAPI_win-x64_Python.zip` / `_win-x86_Python.zip`, plus macOS/Linux builds):
a **.NET 8 C# component** (`YuantaSparkAPI.dll`), driven from Python via
[`pythonnet`](https://pypi.org/project/pythonnet/) (`clr.AddReference("YuantaSparkAPI")`,
`from YuantaOneAPI import YuantaSparkAPITrader, enumEnvironmentMode, enumLogType`). The
docs site's own 說明事項 states: "YuantaSparkAPI元件使用.NET8 C#開發，支援Windows、
Linux、MacOS環境" and "電腦環境請用戶自行安裝 .NET8的SDK" — the .NET 8 SDK is a host
prerequisite, not a pip package.

Critically, **the official Python component ships both `win-x64` and `win-x86` zips**
(plus `osx-arm64`, `osx-x64`, `linux-x64`). Unlike the legacy quote OCX (32-bit only,
the sole reason the whole project was pinned to x32), nothing in SPARK API forces a
bitness choice.

## Decision

- **Python 3.11** stays pinned exactly as before (`requires-python = "==3.11.*"`,
  `.python-version`) — nothing about the vendor pivot changes this reasoning: 3.9 (an
  old vendor sample's version, now moot) is EOL, 3.13 was still judged too new for
  confident wheel coverage of this project's other dependencies (`wxPython`) when
  Feature 01/02 were built, and 3.11 remains in security maintenance until October
  2027.
- **Drop the system-wide x32 constraint.** The whole project now targets **x64
  (64-bit)** — a single venv, no dual-bitness split, matching the modern default for a
  Windows desktop app and avoiding legacy 32-bit toolchain friction (this is the
  opposite of the old constraint's default, chosen because nothing forces 32-bit
  anymore, not because 64-bit was independently mandated). If a future need for 32-bit
  ever arises, SPARK API's own `win-x86` build makes that possible without any
  vendor-side blocker — it would be a from-scratch decision, not a re-adoption of the
  old constraint.
- **New host prerequisite: .NET 8 SDK**, installed system-wide (not via pip) —
  required for `pythonnet`'s CLR hosting to load `YuantaSparkAPI.dll`. Document this in
  the README's setup steps alongside the Python interpreter.
- **New dependency: `pythonnet`** (CLR interop) replaces `comtypes` entirely —
  `comtypes` was exclusively used for the legacy OCX's COM dispatch/event-sink
  machinery (see the old ADR 0004, now superseded), which no longer exists in this
  codebase. `keyring` and `wxPython` are unaffected by this pivot.

## Consequences

- `pyproject.toml`: remove `comtypes`; add `pythonnet>=3,<4` (Windows-only extra,
  matching how `comtypes`/`keyring` were scoped). CI (`actions/setup-python`) drops the
  `architecture: "x86"` pin and its 32-bit runner-specific handling; a plain x64
  `windows-latest` runner is sufficient. The .NET 8 SDK must be available on CI runners
  too if any test actually loads `YuantaSparkAPI.dll` (see the new
  `infrastructure/yuanta/README.md` for what's mock-only vs. real-DLL-touching, mirrors
  the old opt-in `TFX_QUANT_OCX_ACTIVATION_TEST` pattern).
- Every prior x32-specific finding in this repo (the `msvcr90.dll`/MFC DLL-search-path
  bugs, per-user `HKEY_CURRENT_USER` COM registration, `AtlAxCreateControlEx` argument
  handling) is **specific to the retired legacy OCX path** and no longer load-bearing —
  don't carry those fixes forward into the SPARK API adapter; they solved problems that
  don't exist in the new architecture. See the rewritten ADR 0002/0004 for what
  replaces them.
- Vendor deliverables (the SPARK API zip, containing `YuantaSparkAPI.dll` and its
  `.so`/`.dylib` siblings plus a `FunctionList.xls`) are proprietary redistributables —
  same handling as before: installed locally, gitignored, never committed. Exact local
  path convention is recorded in `infrastructure/yuanta/README.md`, not here.
