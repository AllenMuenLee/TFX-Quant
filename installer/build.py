"""Repeatable Windows build for tfx-quant.

Produces a self-contained ``stage/app`` tree — an embedded 32-bit CPython 3.11,
the locked runtime dependencies, and the application source — plus the manifests
the release process and a client-site audit need
(``build-manifest.json``, ``SHA256SUMS``, ``third_party_licenses.txt``,
``RELEASE-NOTES-<version>.md``).

Run with a **32-bit Python 3.11** interpreter (the same one the app ships), e.g.::

    py -3.11-32 installer\\build.py --output installer\\_build

Network is used only to fetch the embeddable Python archive and the wheels, both
checksum-pinned. For a clean-room / offline rebuild, pre-populate
``installer\\_cache`` (or pass ``--python-embed``) and a wheelhouse
(``--wheelhouse <dir> --offline``).

The vendor Yuanta OCX components are never staged or redistributed — the client
installs the 交易 API / 行情 API separately (see ``docs/installation-manual.md``).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tfx_quant import __version__ as APP_VERSION  # noqa: E402
from tfx_quant.packaging.build_support import (  # noqa: E402
    BuildError,
    ToolVersions,
    build_manifest,
    collect_license_inventory,
    render_license_text,
    render_release_notes,
    resolve_python_embed_zip,
    resolve_source_revision,
    rewrite_embed_pth,
    write_build_manifest,
    write_sha256sums,
)

_LAUNCHER_PYW = '''\
"""Windowed entry point for the installed build."""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

# A named mutex the installer/uninstaller detect (installer.iss AppMutex) so an
# upgrade can ask the operator to close a running instance before files change.
if sys.platform == "win32":
    import ctypes

    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "tfx_quant_desktop_singleton")

_config = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "tfx_quant"
    / "config"
    / "settings.json"
)
sys.argv = ["tfx-quant-desktop", *([str(_config)] if _config.is_file() else [])]
runpy.run_module("tfx_quant.desktop", run_name="__main__")
'''

_LAUNCHER_CMD = '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0launcher.pyw" %*\r\n'


def _run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _pip_version() -> str:
    out = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True, text=True, check=True
    ).stdout
    return out.split()[1] if out.split() else "unknown"


def build(args: argparse.Namespace) -> Path:
    if not args.allow_any_bitness and (sys.maxsize > 2**32):
        raise BuildError(
            "run installer/build.py with a 32-bit Python 3.11 (the bundled runtime is "
            "32-bit); pass --allow-any-bitness only for a manifest/lint dry run"
        )

    output = Path(args.output).resolve()
    stage = output / "stage"
    app = stage / "app"
    if stage.exists():
        shutil.rmtree(stage)
    app.mkdir(parents=True)

    lock = REPO_ROOT / "installer" / "requirements.lock"
    lock_text = lock.read_text(encoding="utf-8")

    print("[1/7] embedded CPython")
    embed_zip = resolve_python_embed_zip(
        cache_dir=Path(args.cache_dir).resolve(),
        explicit_path=Path(args.python_embed).resolve() if args.python_embed else None,
        allow_download=not args.offline,
    )
    runtime = app / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(embed_zip) as archive:
        archive.extractall(runtime)

    print("[2/7] locked dependencies -> Lib/site-packages")
    site_packages = app / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    pip_cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--requirement",
        str(lock),
        "--require-hashes",
        "--no-deps",
        "--only-binary",
        ":all:",
    ]
    if args.wheelhouse:
        pip_cmd += ["--no-index", "--find-links", str(Path(args.wheelhouse).resolve())]
    _run(pip_cmd)

    print("[3/7] application source (bundled *.example.json data files come with it)")
    shutil.copytree(
        REPO_ROOT / "src" / "tfx_quant",
        app / "src" / "tfx_quant",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    print("[4/7] launcher + path file")
    (app / "launcher.pyw").write_text(_LAUNCHER_PYW, encoding="utf-8")
    (app / "tfx-quant-desktop.cmd").write_text(_LAUNCHER_CMD, encoding="utf-8", newline="")
    rewrite_embed_pth(runtime, ["..\\Lib\\site-packages", "..\\src"])

    print("[5/7] third-party license inventory")
    inventory = collect_license_inventory(site_packages)
    (app / "third_party_licenses.txt").write_text(render_license_text(inventory), encoding="utf-8")

    print("[6/7] build manifest + release notes")
    revision, dirty = resolve_source_revision(REPO_ROOT)
    manifest = build_manifest(
        app_version=APP_VERSION,
        source_revision=revision,
        source_dirty=dirty,
        tools=ToolVersions(python=sys.version.split()[0], pip=_pip_version()),
        python_embed_zip=embed_zip,
        requirements_lock_text=lock_text,
        stage_dir=app,
    )
    write_build_manifest(manifest, app / "build-manifest.json")
    built_at = manifest["built_at_utc"]
    template = (REPO_ROOT / "installer" / "RELEASE-NOTES.template.md").read_text(encoding="utf-8")
    notes = render_release_notes(
        template,
        {
            "version": APP_VERSION,
            "date": built_at[:10] if isinstance(built_at, str) else "",
            "source_revision": revision,
            "dirty": "是 (WARNING)" if dirty else "否",
        },
    )
    (app / f"RELEASE-NOTES-{APP_VERSION}.md").write_text(notes, encoding="utf-8")

    print("[7/7] SHA256SUMS")
    count = write_sha256sums(app, app / "SHA256SUMS")
    if dirty:
        print("  WARNING: working tree is dirty; this is not a clean release build")
    print(f"\nStaged {count} files -> {app}")
    print(f"  source revision : {revision}{' (dirty)' if dirty else ''}")
    print(f"  app version     : {APP_VERSION}")
    return stage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="installer/build.py")
    parser.add_argument("--output", default=str(REPO_ROOT / "installer" / "_build"))
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / "installer" / "_cache"))
    parser.add_argument("--wheelhouse", default=None, help="offline wheel directory")
    parser.add_argument("--python-embed", default=None, help="path to python-*-embed-win32.zip")
    parser.add_argument("--offline", action="store_true", help="never download")
    parser.add_argument(
        "--allow-any-bitness",
        action="store_true",
        help="skip the 32-bit interpreter guard (manifest/lint dry runs only)",
    )
    args = parser.parse_args(argv)
    try:
        build(args)
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
