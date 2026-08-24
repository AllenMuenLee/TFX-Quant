# TfxQuant

A Windows desktop automated trading system for Yuanta Futures (元大期貨), scoped to
one instrument at a time (小台指 MXF or 大台指 TXF), max net position 2 lots, no
overnight positions (flat by 04:55 Asia/Taipei daily). This repository currently
contains **Feature 01 — Solution Foundation** (layered Python skeleton, domain model,
event coordination, configuration/validation, safety gate), **Feature 02 — Yuanta API
Session** (real login/session-lifecycle adapter for the trading + quote OCXs,
capability model, credentials, preflight checks), and **Feature 03 — Instrument and
Contract Selection** (controlled 商品主檔, AUTO/MANUAL contract selection with
mandatory confirmation, guarded instrument/contract switching, quote/position/order
consistency check). See `implementation prompt/README.md` for the full 16-feature
roadmap.

**Platform constraint**: the entire project — every layer, not just the Yuanta
adapter — is **x32 (32-bit) only**, per an explicit client requirement (the quote OCX
has no 64-bit build). There is no x64 build/dev/test/CI path anywhere in this repo.
See `docs/adr/0001-python-version-and-runtime.md`.

Built in **Python** (mandatory per `implementation prompt/README.md`) —
`wxPython` + `comtypes` for the desktop UI and future COM/OCX integration.

## Architecture

```
tfx_quant.desktop ─────┐
                        ├──> tfx_quant.application ──> tfx_quant.domain
tfx_quant.persistence ──┘
tfx_quant.infrastructure (incl. .yuanta) ──> tfx_quant.application ──> tfx_quant.domain
```

- `docs/adr/` — the "why": Python version/runtime, UI framework/COM hosting,
  layering and event/threading model.
- `docs/modules.md` — the "what's in each package".
- `docs/secrets-management.md` — how broker credentials are handled (never in
  source, never in `settings.example.json`, never in general logs).
- `src/tfx_quant/infrastructure/yuanta/README.md` — the vendor API package
  inventory (ProgIDs, bitness, endpoints).

## Prerequisites

- **Python 3.11, 32-bit (x32)** — pinned via `.python-version` / `requires-python` in
  `pyproject.toml`. This is the **only** supported interpreter bitness for this
  project (see the platform constraint above and
  `docs/adr/0001-python-version-and-runtime.md`) — do not use a 64-bit Python here.
  If you don't already have a 32-bit Python 3.11 installed:
  `winget install --id Python.Python.3.11 --architecture x86` (no admin required;
  lands at `%LocalAppData%\Programs\Python\Python311-32\python.exe`, and registers as
  `py -3.11-32` with the `py` launcher if a 64-bit 3.11 isn't already claiming
  `-3.11`).
- **Yuanta API packages** (optional — only needed for the real, non-mock gateways).
  Not committed to this repository; copy the vendor's component folders to
  `C:\Yuanta\API` and `C:\Yuanta\QAPI` (the vendor's own documented layout) per
  `src/tfx_quant/infrastructure/yuanta/README.md`. Nothing in this repository
  requires them to build or test — see `use_mock: true` in
  `src/tfx_quant/desktop/settings.example.json`. **Verified working end-to-end**
  against the real files this session (2026-08-16, including a live network
  round-trip) — no admin rights needed; `composition.py` registers the components
  per-user automatically. See `docs/adr/0004-broker-session-architecture.md` for the
  full trail and what's still unverified (a full login needs real credentials).

## Install, lint, type-check, test, run

```powershell
py -3.11-32 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

# Lint and format
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .

# Type-check (src only)
.venv\Scripts\mypy src

# Architecture/layering check (dependencies point toward domain)
.venv\Scripts\lint-imports

# Full test suite
.venv\Scripts\pytest

# Run the desktop diagnostics screen (shows per-module readiness; sends no orders)
.venv\Scripts\python -m tfx_quant.desktop
```

No Yuanta credentials or OCX installation are required for any of the above — the mock
gateways (`use_mock: true`, the default in `settings.example.json`) let CI and any dev
machine build and test without the broker API present, per the acceptance requirement.
Setting `use_mock: false` wires the real session (`BrokerSessionOrchestrator` + the
comtypes/wx OCX adapters) instead — see `docs/secrets-management.md` for the
credential env var/Windows Credential Manager setup it needs, and
`docs/adr/0004-broker-session-architecture.md` for its current execution status.

## Opt-in real-execution tests

Two test files make real network calls and are skipped by default (never run in CI,
which has neither the vendor files nor real credentials):

- `tests/infrastructure/test_yuanta_ocx_activation.py` —
  `TFX_QUANT_OCX_ACTIVATION_TEST=1` (auto-skips if the vendor `.ocx` files aren't
  present). Uses placeholder credentials; only proves the real activation/
  registration/invocation/event-delivery pipeline works, not that login succeeds.
- `tests/infrastructure/test_yuanta_live_smoke.py` —
  `TFX_QUANT_LIVE_YUANTA_SMOKE_TEST=1`, plus real credentials (see
  `docs/secrets-management.md`). Asserts a full `BrokerSessionReady`.

## Configuration

`src/tfx_quant/desktop/settings.example.json` is the sample, non-secret configuration
(`account_alias`, `environment`, `selected_instrument`, `contract_selection_mode`,
`timezone_id`, `eod_flatten_local_time`, `max_net_lots`). It is validated
on load (`TradingSettings` + `validate_startup()`) — a misconfigured value (wrong time
zone, wrong flatten time, lot cap above 2, undefined instrument, incomplete manual
contract selection) fails immediately with a clear message instead of letting the
strategy engine run with bad configuration.

Broker credentials are never part of this file — see `docs/secrets-management.md`.

## Safety rules this codebase enforces

- `Quantity` refuses to represent more than 2 lots; `NetPosition` refuses a net
  position above 2 lots in either direction (`domain/quantity.py`).
- `Price`/`Money` are `decimal.Decimal`-backed value objects; there is no path to
  store an amount as `float` (`domain/money.py`).
- `StrategyStateMachine` only allows the exact transition table in
  `domain/strategy_state.py` — in particular, `Running` is only reachable from
  `Starting`, never directly from `Stopped` or `PausedSafe`.
- `StartupSafetyGate.can_enter_running()` additionally requires every item in
  `SafetyChecklist` (API logged in, account confirmed, market data valid, order
  query completed, position synced, no unknown orders, user pressed start) before
  allowing the transition to `Running`.
- Every order carries a `ClientOrderId` (a UUID-backed idempotency key) independent
  of whatever order number the broker assigns (`domain/order.py`).
- `EventCoordinator` serializes all broker-callback/UI/strategy-thread traffic onto a
  single processing loop so subscriber code never races itself (see ADR 0003).
- No code path anywhere in this codebase can submit an order yet — the domain layer
  has no order-sending type or method (that's Feature 06), and the desktop screen's
  Connect/Disconnect/account-picker controls (Feature 02) are session-lifecycle
  actions, not order submission.
- `SessionCapabilities` (Feature 02) keeps login/market-data/trading/order-reports/
  queries as five independent booleans — logging in is never treated as "can trade"
  (`application/ports/broker_session.py`).

## CI

`.github/workflows/ci.yml` sets up a 32-bit (x32) Python 3.11, installs `.[dev]`, and
runs `ruff`, `mypy`, `lint-imports`, and `pytest` on `windows-latest` using only the
mock Yuanta gateways — no broker credentials or OCX installation are configured or
needed.
