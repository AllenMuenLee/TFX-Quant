"""Deployment-support code that ships *inside* the application.

Feature 16 (Windows installer, source delivery, documentation). Three concerns,
each importable and unit-tested rather than buried in an Inno Setup ``[Code]``
section:

- :mod:`tfx_quant.packaging.prechecks` — read-only environment checks (Windows
  version, free disk, 32-bit VC++ runtime, whether the Yuanta 交易/行情 OCX
  packages look installed and registered).
- :mod:`tfx_quant.packaging.install_log` — the dedicated installer/updater debug
  log required by the prompt's "除錯日誌需求": newline-delimited JSON, every field
  masked so no credential, certificate password, or full user path is written.
- :mod:`tfx_quant.packaging.migrate` — the pre-upgrade data-safety step:
  integrity-check every SQLite database under the per-user data directory, refuse
  an upgrade when one is corrupt or newer than this build, back every database up
  first so a failed upgrade is recoverable.
- :mod:`tfx_quant.packaging.build_support` — reusable helpers for
  ``installer/build.py`` (manifest, checksums, license inventory).

Submodules are intentionally **not** imported here: each is also run as
``python -m tfx_quant.packaging.<name>`` by the Inno Setup script, and an eager
import would trigger a ``runpy`` re-execution warning.

The vendor OCX components are never redistributed by this project, so nothing here
copies, registers, or bundles them — it only *detects* them.
"""

from __future__ import annotations

__all__: list[str] = []
