# ADR 0002 — UI framework and COM/OCX hosting

## Status

Accepted (Feature 01). The "open question" in Consequences below (which thread hosts
the OCX) was resolved in Feature 02 — see `docs/adr/0004-broker-session-
architecture.md`, decision 1: the wx main UI thread, via `AtlAxCreateControlEx`
directly (not `wx.lib.activex.ActiveXCtrl`, corrected below). The wheel-availability
risk flagged below was also resolved (unfounded) in Feature 02.

## Context

The desktop UI must eventually host two ActiveX/OCX controls (Yuanta's trading and
quote APIs — see `src/tfx_quant/infrastructure/yuanta/README.md`) and must not send
orders automatically on startup. `implementation prompt/01-solution-foundation/
implementation-prompt.md` requires first reviewing the vendor API docs/samples, then
deciding the Python UI framework and recording rationale here.

The vendor's own Python sample (see ADR 0001) uses **wxPython + comtypes**, hosting
the OCX via `AtlAxCreateControlEx` with a ProgID that must match interpreter bitness.
`wx.lib.activex.ActiveXCtrl` is wxPython's documented, maintained way to embed an
ActiveX control as a child of a `wx.Window` — there is no equivalent first-class
ActiveX-hosting story in Tkinter or PySide6/PyQt6 on Windows.

## Decision

- **wxPython** for all desktop UI.
- **comtypes** for COM/OCX access (matches the vendor sample; MIT-licensed, pure
  Python, well-suited to sink-based event handling for `OnOrdRptF`/`OnGetMktAll`/etc.
  in Feature 02+).
- Feature 01 ships only a **read-only startup diagnostics screen**
  (`desktop/readiness_frame.py`) — a `wx.Frame` displaying per-module readiness
  booleans/labels. It has no button, menu, or code path that can submit an order; the
  domain layer itself has no order-sending type or method at all yet.

## Consequences

- `wxPython>=4.2` and `comtypes>=1.2` are declared dependencies (newer than the
  vendor's 4.1.1/1.1.11 — deliberately not pinned to those EOL-adjacent versions).
- The `EventCoordinator` (ADR 0003) is deliberately UI-framework-agnostic so whichever
  thread the COM callback fires on, it converts to an internal `Event` and hands off
  to the coordinator immediately, keeping the OCX-hosting-thread decision from leaking
  into `application`/`domain`. See ADR 0004 for how that thread was actually decided.
- **Risk flagged in Feature 01, resolved in Feature 02**: wxPython's official PyPI
  wheels were flagged as possibly not covering 32-bit (x32) Windows for recent
  releases. As of Feature 02 (2026-08-16), `pip install wxPython` under a real x32
  Python 3.11 interpreter resolved and imported wxPython 4.3.1 with no issue — the
  risk didn't materialize, no source build or fallback architecture was needed. (The
  "restrict wxPython to an x64 process, host the OCX in a separate x86 helper" fallback
  this ADR originally sketched is now moot for a different reason too: per ADR 0001's
  Feature 02 revision, the whole project is x32-only — there is no x64 process
  anywhere in this system to host wxPython in.)
