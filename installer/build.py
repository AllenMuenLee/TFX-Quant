"""Repeatable Windows build for tfx-quant.

Produces a self-contained ``stage/app`` tree — an embedded 32-bit CPython 3.11,
the locked runtime dependencies, and the application source — plus the manifests
the release process and a client-site audit need
(``build-manifest.json``, ``SHA256SUMS``, ``third_party_licenses.txt``,
``RELEASE-NOTES-<version>.md``).

Run with a **32-bit Python 3.11** interpreter (the same one the app ships), e.g.::

    py -3.11-32 installer\\build.py --output installer\\_build

Network is used only to fetch the embeddable Python archive, the wheels, and the
Microsoft VC++ x86 redistributable — all checksum-pinned. For a clean-room /
offline rebuild, pre-populate ``installer\\_cache`` (or pass ``--python-embed`` /
``--vcredist``) and a wheelhouse (``--wheelhouse <dir> --offline``).

Vendor payload (opt-out with ``--no-vendor``): when the Yuanta component folders
are present (``交易API元件及說明文件/API`` and ``行情API元件及說明文件/.../QAPI``, or
``--yuanta-api-dir`` / ``--yuanta-qapi-dir``), they are staged under
``stage/app/vendor`` so the installer can copy them to ``C:\\Yuanta`` and register
the OCX. These are Yuanta's proprietary components — bundling them asserts that the
distributor holds a redistribution right from Yuanta.
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
    copy_vendor_tree,
    render_license_text,
    render_release_notes,
    resolve_python_embed_zip,
    resolve_source_revision,
    resolve_vcredist,
    rewrite_embed_pth,
    sha256_file,
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


def _default_yuanta_dir(*parts: str) -> Path | None:
    candidate = REPO_ROOT.joinpath(*parts)
    return candidate if candidate.is_dir() else None


def _stage_vendor(app: Path, args: argparse.Namespace) -> dict[str, object] | None:
    """Stage the VC++ redistributable + (optionally) the Yuanta OCX payload under
    ``app/vendor``. Returns the manifest fragment, or ``None`` when nothing was
    bundled."""
    if args.no_vendor:
        print("[vendor] skipped (--no-vendor)")
        return None

    vendor = app / "vendor"
    fragment: dict[str, object] = {
        "redistribution_right_asserted_by_distributor": True,
        "note": (
            "Yuanta components are proprietary to 元大期貨股份有限公司; bundling them "
            "asserts a redistribution right held by whoever runs this build."
        ),
    }
    staged_any = False

    try:
        vcredist = resolve_vcredist(
            cache_dir=Path(args.cache_dir).resolve(),
            explicit_path=Path(args.vcredist).resolve() if args.vcredist else None,
            allow_download=not args.offline,
        )
        vendor.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vcredist, vendor / "vc_redist.x86.exe")
        fragment["vcredist"] = {
            "filename": "vc_redist.x86.exe",
            "sha256": sha256_file(vendor / "vc_redist.x86.exe"),
        }
        staged_any = True
        print("[vendor] vc_redist.x86.exe staged")
    except BuildError as exc:
        print(f"[vendor] vc_redist not bundled: {exc}")

    api_dir = (
        Path(args.yuanta_api_dir).resolve()
        if args.yuanta_api_dir
        else _default_yuanta_dir("交易API元件及說明文件", "API")
    )
    qapi_dir = (
        Path(args.yuanta_qapi_dir).resolve()
        if args.yuanta_qapi_dir
        else _default_yuanta_dir("行情API元件及說明文件", "行情API元件及說明文件", "QAPI")
    )

    if api_dir is not None and (api_dir / "YuantaOrd.ocx").is_file():
        n = copy_vendor_tree(api_dir, vendor / "API")
        fragment["yuanta_trade_api"] = {"source": api_dir.name, "file_count": n}
        staged_any = True
        print(f"[vendor] Yuanta trade API staged ({n} files) from {api_dir}")
    else:
        print("[vendor] Yuanta trade API not bundled (folder absent)")

    if qapi_dir is not None and (qapi_dir / "YuantaQuote_v2.1.2.9.ocx").is_file():
        n = copy_vendor_tree(qapi_dir, vendor / "QAPI")
        fragment["yuanta_quote_api"] = {"source": qapi_dir.name, "file_count": n}
        staged_any = True
        print(f"[vendor] Yuanta quote API staged ({n} files) from {qapi_dir}")
    else:
        print("[vendor] Yuanta quote API not bundled (folder absent)")

    if not staged_any:
        return None
    return fragment


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

    print("[1/8] embedded CPython")
    embed_zip = resolve_python_embed_zip(
        cache_dir=Path(args.cache_dir).resolve(),
        explicit_path=Path(args.python_embed).resolve() if args.python_embed else None,
        allow_download=not args.offline,
    )
    runtime = app / "runtime"
    runtime.mkdir()
    with zipfile.ZipFile(embed_zip) as archive:
        archive.extractall(runtime)

    print("[2/8] locked dependencies -> Lib/site-packages")
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

    print("[3/8] application source (bundled *.example.json data files come with it)")
    shutil.copytree(
        REPO_ROOT / "src" / "tfx_quant",
        app / "src" / "tfx_quant",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    print("[4/8] launcher + path file + install-all.bat")
    (app / "launcher.pyw").write_text(_LAUNCHER_PYW, encoding="utf-8")
    (app / "tfx-quant-desktop.cmd").write_text(_LAUNCHER_CMD, encoding="utf-8", newline="")
    shutil.copy2(REPO_ROOT / "installer" / "install-all.bat", app / "install-all.bat")
    rewrite_embed_pth(runtime, ["..\\Lib\\site-packages", "..\\src"])

    print("[5/8] vendor payload (VC++ redist + Yuanta OCX)")
    vendor_fragment = _stage_vendor(app, args)

    print("[6/8] third-party license inventory")
    inventory = collect_license_inventory(site_packages)
    (app / "third_party_licenses.txt").write_text(
        render_license_text(inventory, vendor_bundled=vendor_fragment is not None),
        encoding="utf-8",
    )

    print("[7/8] build manifest + release notes")
    revision, dirty = resolve_source_revision(REPO_ROOT)
    manifest = build_manifest(
        app_version=APP_VERSION,
        source_revision=revision,
        source_dirty=dirty,
        tools=ToolVersions(python=sys.version.split()[0], pip=_pip_version()),
        python_embed_zip=embed_zip,
        requirements_lock_text=lock_text,
        stage_dir=app,
        vendor_bundle=vendor_fragment,
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

    print("[8/8] SHA256SUMS")
    count = write_sha256sums(app, app / "SHA256SUMS")
    if dirty:
        print("  WARNING: working tree is dirty; this is not a clean release build")
    if vendor_fragment is not None:
        print(
            "  NOTE: this build BUNDLES Yuanta proprietary OCX components — it must "
            "only be distributed if you hold a redistribution right from Yuanta."
        )
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
    parser.add_argument("--vcredist", default=None, help="path to vc_redist.x86.exe")
    parser.add_argument("--yuanta-api-dir", default=None, help="folder holding YuantaOrd.ocx")
    parser.add_argument(
        "--yuanta-qapi-dir", default=None, help="folder holding YuantaQuote_v2.1.2.9.ocx"
    )
    parser.add_argument(
        "--no-vendor",
        action="store_true",
        help="do not bundle the VC++ redistributable or any Yuanta component",
    )
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
