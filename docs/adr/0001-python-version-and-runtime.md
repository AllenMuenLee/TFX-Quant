# ADR 0001 — Python version and runtime

## Status

Accepted (Feature 01). **Revised (Feature 02, 2026-08-16)**: the bitness scope was
widened from "just `infrastructure.yuanta`/`desktop`" to the whole system, per the
client's explicit platform constraint added to `implementation prompt/01-solution-
foundation/implementation-prompt.md` and `implementation prompt/02-yuanta-api-
session/implementation-prompt.md`:

> 平台硬性限制：整個系統只能使用 x32；不得使用 x86 或 x64。原因是行情 API 僅提供 x32
> 版本。所有開發、相依套件、執行環境、測試、建置與部署均須遵守此限制。

This repo calls this **x32** throughout, matching that wording — it denotes the same
thing this ADR originally called "x86" (a 32-bit process), not a different concept.
See "Terminology" below.

## Context

`implementation prompt/README.md` mandates Python for the entire system and requires
picking "a version that is still in its security maintenance window and compatible
with the Yuanta API" (原文: "建議選用仍在安全維護期且與元大 API 相容的版本"), fixed
and recorded in project settings.

The vendor's own Python sample, documented in
`交易API元件及說明文件/元大API交易PYTHON注意事項.docx` (text extracted this session
via zip/xml parsing — the file has no runnable sample code, just a compatibility
note), was built on **Python 3.9, wxPython 4.1.1, comtypes 1.1.11**.

## Terminology

This project's docs/code now say **x32** for "32-bit", not "x86" — the client's
implementation prompts use x32 specifically to avoid the ambiguity of "x86" (which
colloquially sometimes gets used for the whole x86/x86-64 family). They mean the same
32-bit process bitness; nothing here is the Linux x32 ABI or any other distinct
concept. Third-party tools that hard-code the string "x86" as their own parameter
value (e.g. `actions/setup-python`'s `architecture: "x86"`, Windows' own "x86" Program
Files folder naming) are left as-is — that's their fixed vocabulary, not ours.

## Decision

Pin **Python 3.11**, 32-bit (x32) build, exactly, via `requires-python = "==3.11.*"`
in `pyproject.toml` and a `.python-version` file — **one single venv for the entire
project**, not a dual x64-dev/x32-production split. `domain`/`application` have no
COM dependency and would function under any bitness, but per the client's explicit
"整個系統只能使用 x32...所有開發、相依套件、執行環境、測試、建置與部署均須遵守此限制"
requirement, there is no x64 build/dev/test/CI path anywhere in this project — a
single interpreter, one venv, one CI job, covers every layer.

- Not 3.9 (the vendor's own version): reached end-of-life October 2025, so it no
  longer satisfies "仍在安全維護期" (still in the security maintenance window).
- Not 3.13 (the newest installed): too new to have confident, well-tested wheel
  coverage for `comtypes`/`wxPython` against this specific vendor OCX integration;
  picking the newest available interpreter isn't itself a requirement.
- 3.11 is in security maintenance until **October 2027**, has mature `comtypes` and
  `wxPython` wheel support (both installed and importable under the x32 interpreter as
  of Feature 02 — see Consequences).
- **Bitness reason** (why x32 specifically, for the whole system): the trading OCX
  ships both 32-bit (`API/YuantaOrd.ocx`, `Yuanta.YuantaOrdCtrl.1`) and 64-bit
  (`API_x64/YuantaOrd64.ocx`, `Yuanta.YuantaOrdCtrl.64`) builds, but the quote OCX
  (`QAPI/YuantaQuote_v2.1.2.9.ocx`) ships **32-bit only**. Both controls must be
  hosted in the same process (see ADR 0002/0004), so that process — and, per the
  client's explicit instruction, the whole project's dev/test/CI toolchain too — must
  be x32.

## Consequences

- **Installed and working** (Feature 02, 2026-08-16): a 32-bit Python 3.11.9
  interpreter (`winget install --id Python.Python.3.11 --architecture x86`, landed at
  `%LocalAppData%\Programs\Python\Python311-32\python.exe`, no admin required), with a
  single project venv at `.venv` built from it. `comtypes`, `wxPython` (4.3.1 — the x32
  wheel-availability risk ADR 0002 flagged turned out to be unfounded, a current
  wxPython release does ship x32 Windows wheels), `pydantic`, and `keyring` all import
  correctly under this interpreter. The full test suite (168 passed, 1 opt-in skipped)
  and `ruff`/`mypy`/`lint-imports` all pass under this sole x32 venv.
- **Still blocked**: actually loading the vendor trading OCX (`YuantaOrd.ocx`) fails —
  `LoadLibrary` on its dependency `msvcr90.dll` (VC++ 2008 runtime) returns
  `WinError 1114` ("A dynamic link library (DLL) initialization routine failed"),
  reproduced both with the vendor-bundled `msvcr90.dll` and a newer one copied from a
  freshly-installed VC++ 2008 SP1/2010 x86 redistributable (`Microsoft.VCRedist.2008.
  x86`, `Microsoft.VCRedist.2010.x86`, both installed via winget without admin). The
  `Microsoft.VC90.MFC` side-by-side assembly (`mfc90.dll`/`mfcm90.dll`) also has no
  WinSxS registration on this machine at all — the standard `vcredist_x86.exe` does not
  install it (MFC security updates for VC90 shipped as a separate, no-longer-available
  hotfix historically). This is a legacy-DLL-compatibility problem specific to this
  ~15-year-old MFC ActiveX control on a modern Windows build, not a Python/bitness
  problem — `struct.calcsize("P")` confirms the interpreter genuinely is 32-bit
  throughout. `regsvr32` registration was not attempted further pending a decision on
  how to resolve this (see `docs/adr/0004-broker-session-architecture.md`'s "What's
  not verified" for the up-to-date status — this file gets stale fastest of anything
  in the repo, check there first).
- No dual-venv setup exists or should be reintroduced — every layer (`domain` through
  `desktop`) builds, lints, type-checks, and tests under the one x32 venv. CI
  (`.github/workflows/ci.yml`) uses `actions/setup-python`'s `architecture: "x86"`
  (the action's own required literal value) to get the same 32-bit interpreter on
  `windows-latest` runners.
