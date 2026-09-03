# TfxQuant

A Windows desktop automated trading system for Yuanta Futures (元大期貨), scoped to
one instrument at a time for trading — **fixed to 小台指 (MXF)** — with a max net
position of 2 lots and no overnight positions (flat by 04:55 Asia/Taipei daily). The
UI's MXF/TXF switch only changes which instrument's market data is *watched*, never
what is *traded*.

Features 01–16 of the roadmap in `implementation prompt/README.md` are implemented:
layered domain model, Yuanta login/session, instrument/contract selection, real-time
market data + 60-minute bars, the 60-minute strategy signal engine, the order/fill
state machine, safe reversal/scaling, position reconciliation, connectivity/safe
pause, risk supervisor + 04:55/emergency flatten, P&L and trade reports, the wxPython
desktop UI, structured logging/telemetry/audit, persistence/recovery, the
simulation (`environment: TEST`) test environment, and (Feature 16) the Windows
installer, source delivery, and documentation.

See [`docs/`](docs/) for the operator and maintainer manuals — start with
[`docs/README.md`](docs/README.md). Known gaps between the shipped software and the
specification (notably: no strategy start/pause/stop UI, and `StartupSafetyGate` is
not wired as a hard gate) are listed in
[`docs/operations-manual.md` §10](docs/operations-manual.md) and
[`docs/maintenance.md` §1](docs/maintenance.md).

## Platform constraint

The entire project — every layer, not just the Yuanta adapter — is **32-bit (x86)
only**, per an explicit client requirement: the Yuanta quote OCX
(`YuantaQuote_v2.1.2.9.ocx`) has no 64-bit build, and the trade OCX
(`YuantaOrd.ocx`) is loaded at the same bitness as the interpreter. There is no
x64 build/dev/test/CI path anywhere in this repo. The Windows installer bundles a
fixed 32-bit CPython 3.11.

The broker integration is the **legacy Yuanta COM/ActiveX OCX pair**, hosted via
`comtypes` + `AtlAxCreateControlEx` on the wx UI thread. (An earlier revision
pivoted the codebase to the newer "SPARK" .NET API; that pivot was **reverted** —
no SPARK code exists in `src/`.) The API contract source of truth is the vendor
material under `交易API元件及說明文件/` (trade) and `行情API元件及說明文件/`
(quote), which are gitignored and installed locally.

Built in **Python** (mandatory per `implementation prompt/README.md`) —
`wxPython` + `comtypes` for the desktop UI and COM/OCX integration, `pydantic` for
validated settings, `keyring` for the optional secure password store.

## Architecture

```
tfx_quant.desktop ─────┐
                        ├──> tfx_quant.application ──> tfx_quant.domain
tfx_quant.persistence ──┘
tfx_quant.infrastructure (incl. .yuanta) ──> tfx_quant.application ──> tfx_quant.domain
tfx_quant.telemetry    — importable from any layer
tfx_quant.packaging    — installer/updater support code (see installer/)
```

- `docs/maintenance.md` — architecture, databases, settings, logging, the API
  version matrix, build/test, troubleshooting.
- `src/tfx_quant/infrastructure/yuanta/README.md` — the vendor OCX inventory
  (ProgIDs, OCX names, bitness, endpoints).
- `src/tfx_quant/desktop/composition.py` — the composition root.
- `installer/` — the repeatable Windows build, installer, and release tooling.

## Prerequisites

- **Python 3.11, 32-bit (x86)** — pinned via `.python-version` / `requires-python`
  in `pyproject.toml`. This is the **only** supported interpreter bitness. If you
  don't already have one:
  `winget install --id Python.Python.3.11 --architecture x86` (no admin required;
  registers as `py -3.11-32`).
- **Yuanta API packages** (optional — only for the real, non-mock gateways). Not
  committed; copy the vendor's component folders to `C:\Yuanta\API` and
  `C:\Yuanta\QAPI` and run `install_YTFutOrdAP.bat` / `install_ytocx.bat` as
  Administrator. See `docs/installation-manual.md` §2 and
  `src/tfx_quant/infrastructure/yuanta/README.md`. Nothing in this repository
  requires them to build or test — `environment: TEST` uses the mock trade gateway.

## Install, lint, type-check, test, run

```powershell
py -3.11-32 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
.venv\Scripts\mypy src
.venv\Scripts\lint-imports
.venv\Scripts\pytest

# Run the desktop app (readiness screen; TEST env sends no real orders)
.venv\Scripts\python -m tfx_quant.desktop
```

No Yuanta credentials or OCX installation are required for any of the above — the
mock trade gateway (`environment: TEST` in `settings.example.json`) lets CI and any
dev machine build and test without the broker API present. `environment:
PRODUCTION` wires the real `LegacyBroker` OCX adapter (trade) instead; market data
is the real Yuanta quote OCX in **both** environments.

## Configuration

`src/tfx_quant/desktop/settings.example.json` is the sample, non-secret config
(`account_alias`, `environment`, `selected_instrument`, `contract_selection_mode`,
`timezone_id`, `eod_flatten_local_time`, `max_net_lots`, and optional per-database
paths). It is validated on load (`TradingSettings` + `validate_startup()`); a
misconfigured value fails immediately with a clear message. Broker credentials are
never part of this file — they are entered in the login dialog and, only if the
operator opts in, stored in Windows Credential Manager via `keyring`. See
`docs/maintenance.md` §3.

## Opt-in real-execution tests

Skipped by default (never run in CI):

- `tests/infrastructure/test_yuanta_ocx_activation.py` —
  `TFX_QUANT_OCX_ACTIVATION_TEST=1` (auto-skips without the vendor `.ocx` files).
- `tests/infrastructure/test_yuanta_live_smoke.py` —
  `TFX_QUANT_LIVE_YUANTA_SMOKE_TEST=1` plus real credentials.
- Anything marked `real_api` — `TFX_QUANT_REAL_API=1` (never sends orders).

## Installer / release

```powershell
py -3.11-32 installer\build.py            # stage app + manifests + checksums + licenses
py -3.11-32 installer\make_installer.py   # compile tfx-quant-setup.exe (needs Inno Setup 6), optionally sign
```

`build.py` also bundles a **vendor payload** (Microsoft VC++ x86 redistributable +
the Yuanta 交易/行情 OCX, when their folders are present) so the installer can set
up `C:\Yuanta` and register the OCX behind one UAC prompt. Bundling Yuanta's
proprietary components requires a redistribution right from Yuanta; use
`build.py --no-vendor` for a components-free installer. See
[`installer/README.md`](installer/README.md) and `docs/acceptance-checklist.md`.

## CI

`.github/workflows/ci.yml` runs on `windows-latest` with a **32-bit (x86)** Python
3.11: `ruff`, `mypy`, `lint-imports`, `pytest` (mock gateways only, no broker
credentials or OCX), plus a `package` job that runs `installer/build.py`.
