# installer/ — build, package, sign, release

Repeatable Windows delivery for tfx-quant (Feature 16). Nothing here bundles the
proprietary Yuanta OCX components — the client installs the 交易 API / 行情 API
separately (see [`../docs/installation-manual.md`](../docs/installation-manual.md)).

## Contents

| Path | What it is |
|---|---|
| `build.py` | Stages the app: embedded 32-bit CPython 3.11 + locked deps + source + manifests. |
| `make_installer.py` | Compiles `installer.iss` with Inno Setup, optionally signs, writes `release-manifest.json`. |
| `installer.iss` | Inno Setup 6 script (per-user install, pre-checks, safe upgrade, safe uninstall). |
| `requirements.in` / `requirements.lock` | Pinned, hashed runtime dependency set (mirrors `pyproject.toml`). |
| `RELEASE-NOTES.template.md` | Rendered per release by `build.py`. |
| `_cache/`, `_build/` | Download cache and build output — git-ignored. |

## Prerequisites

- **32-bit Python 3.11** (`py -3.11-32`) — the same interpreter the app ships.
- **Inno Setup 6** (`ISCC.exe` on PATH or in `C:\Program Files (x86)\Inno Setup 6`)
  — only needed for `make_installer.py`.
- Optional: a code-signing certificate + the Windows SDK's `signtool.exe`.

## Build a release

```powershell
py -3.11-32 installer\build.py --output installer\_build
py -3.11-32 installer\make_installer.py
```

Outputs under `installer\_build`:

```
stage\app\                     the packaged application tree
  build-manifest.json          source revision, tool + dependency versions & hashes
  SHA256SUMS                    one line per staged file
  third_party_licenses.txt     inventory (Yuanta components are NOT listed / shipped)
  RELEASE-NOTES-<version>.md
dist\
  tfx-quant-setup.exe
  release-manifest.json         installer sha256 ↔ source revision ↔ build manifest ↔ signature
  SHA256SUMS
```

## Clean-room / offline rebuild

1. Copy `python-3.11.9-embed-win32.zip` into `installer\_cache\` (or pass
   `--python-embed <path>`). Its SHA-256 is pinned in
   `tfx_quant/packaging/build_support.py` and verified on every build.
2. Populate a wheelhouse: `pip download -r installer\requirements.lock -d <dir>
   --only-binary :all: --platform win32 --python-version 311`.
3. `py -3.11-32 installer\build.py --offline --wheelhouse <dir>`.

The `build-manifest.json` from two clean builds of the same commit is identical
except for `built_at_utc`.

## Code signing (opt-in)

`make_installer.py` signs the finished `.exe` only when configured via environment
variables, otherwise it logs `signing skipped` and produces an unsigned installer:

| Variable | Meaning |
|---|---|
| `TFX_QUANT_SIGN_THUMBPRINT` | SHA-1 thumbprint of a cert in the Windows store |
| `TFX_QUANT_SIGN_PFX` + `TFX_QUANT_SIGN_PFX_PASSWORD` | a `.pfx` file and its password |
| `TFX_QUANT_SIGN_TIMESTAMP_URL` | RFC-3161 timestamp URL (default: DigiCert) |

## Upgrade / rollback behaviour

`installer.iss` runs, via the *previous* build's bundled Python, before replacing
any file:

```
runtime\python.exe -m tfx_quant.packaging.migrate --apply --log <installer log>
```

which integrity-checks every `%LOCALAPPDATA%\tfx_quant\*.sqlite3`, copies them to
`%LOCALAPPDATA%\tfx_quant\backup\pre-upgrade-<ts>\`, and exits non-zero (aborting
the upgrade, old version intact) on corruption or a newer-than-supported schema.
Manual rollback:

```
runtime\python.exe -m tfx_quant.packaging.migrate --restore-latest
```
