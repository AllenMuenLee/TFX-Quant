"""Compile ``installer.iss`` into a versioned Windows installer, optionally sign it,
and write the release manifest that ties it back to the source revision.

Prerequisites: a completed ``installer/build.py`` stage tree, and Inno Setup 6
(``ISCC.exe``) on PATH or in its default location. Code signing is opt-in and only
happens when the ``TFX_QUANT_SIGN_*`` environment variables are set — otherwise the
step is skipped with a logged notice (the installer is still produced, unsigned).

    TFX_QUANT_SIGN_THUMBPRINT   SHA-1 thumbprint of a cert in the user/machine store
    -- or --
    TFX_QUANT_SIGN_PFX          path to a .pfx
    TFX_QUANT_SIGN_PFX_PASSWORD its password
    TFX_QUANT_SIGN_TIMESTAMP_URL RFC-3161 URL (default: DigiCert)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tfx_quant.packaging.build_support import (  # noqa: E402
    make_release_manifest,
    sha256_file,
)

_ISCC_DEFAULTS = (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
)
_SIGNTOOL_SDK_ROOT = r"C:\Program Files (x86)\Windows Kits\10\bin"
_DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"


def find_iscc(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    on_path = shutil.which("iscc") or shutil.which("ISCC")
    if on_path:
        return on_path
    return next((p for p in _ISCC_DEFAULTS if Path(p).is_file()), None)


def find_signtool() -> str | None:
    on_path = shutil.which("signtool")
    if on_path:
        return on_path
    base = Path(_SIGNTOOL_SDK_ROOT)
    if base.is_dir():
        for arch in ("x86", "x64"):
            found = sorted(base.glob(f"*/{arch}/signtool.exe"))
            if found:
                return str(found[-1])
    return None


def _sign_config() -> dict[str, str] | None:
    thumb = os.environ.get("TFX_QUANT_SIGN_THUMBPRINT")
    pfx = os.environ.get("TFX_QUANT_SIGN_PFX")
    pw = os.environ.get("TFX_QUANT_SIGN_PFX_PASSWORD")
    ts = os.environ.get("TFX_QUANT_SIGN_TIMESTAMP_URL", _DEFAULT_TIMESTAMP_URL)
    if thumb:
        return {"mode": "thumbprint", "value": thumb, "timestamp": ts}
    if pfx and pw:
        return {"mode": "pfx", "value": pfx, "password": pw, "timestamp": ts}
    return None


def sign_installer(exe: Path) -> str | None:
    """Returns the certificate subject on success, ``None`` if signing was skipped.
    Raises ``subprocess.CalledProcessError`` on an actual signing failure."""
    config = _sign_config()
    if config is None:
        print("  signing skipped: no TFX_QUANT_SIGN_* environment variables set")
        return None
    signtool = find_signtool()
    if signtool is None:
        raise FileNotFoundError("signtool.exe not found (install the Windows 10/11 SDK)")
    cmd = [signtool, "sign", "/fd", "SHA256", "/tr", config["timestamp"], "/td", "SHA256"]
    if config["mode"] == "thumbprint":
        cmd += ["/sha1", config["value"]]
    else:
        cmd += ["/f", config["value"], "/p", config["password"]]
    cmd.append(str(exe))
    print("  $ signtool sign ... (credentials redacted)")
    subprocess.run(cmd, check=True)
    subprocess.run([signtool, "verify", "/pa", str(exe)], check=True)
    return "signed (subject not extracted)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="installer/make_installer.py")
    default_stage = REPO_ROOT / "installer" / "_build" / "stage" / "app"
    parser.add_argument("--stage-app", default=str(default_stage))
    parser.add_argument("--output", default=str(REPO_ROOT / "installer" / "_build" / "dist"))
    parser.add_argument("--iscc", default=None)
    parser.add_argument("--skip-if-no-iscc", action="store_true")
    args = parser.parse_args(argv)

    stage_app = Path(args.stage_app).resolve()
    output = Path(args.output).resolve()
    build_manifest_path = stage_app / "build-manifest.json"
    if not build_manifest_path.is_file():
        print(f"missing {build_manifest_path}; run installer/build.py first", file=sys.stderr)
        return 1
    stage_manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
    app_version = str(stage_manifest.get("app_version", "0.0.0"))

    iscc = find_iscc(args.iscc)
    if iscc is None:
        msg = "ISCC.exe (Inno Setup 6) not found on PATH or in Program Files"
        if args.skip_if_no_iscc:
            print(f"  {msg} — skipping installer compilation (--skip-if-no-iscc)")
            return 0
        print(msg, file=sys.stderr)
        return 1

    output.mkdir(parents=True, exist_ok=True)
    iss = REPO_ROOT / "installer" / "installer.iss"
    cmd = [
        iscc,
        f"/DAppVersion={app_version}",
        f"/DStageApp={stage_app}",
        f"/DOutputDir={output}",
    ]
    vendor_bundled = (stage_app / "vendor").is_dir() and (stage_app / "install-all.bat").is_file()
    if vendor_bundled:
        cmd.append("/DBundleVendor")
        print("  vendor payload present -> installer will offer elevated Yuanta/VC++ setup")
    cmd.append(str(iss))
    print("[1/3] compile installer")
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)

    installer_exe = output / "tfx-quant-setup.exe"
    if not installer_exe.is_file():
        print(f"expected {installer_exe} not produced", file=sys.stderr)
        return 1

    print("[2/3] code signing")
    subject: str | None = None
    signed = False
    try:
        subject = sign_installer(installer_exe)
        signed = subject is not None
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"signing failed: {exc}", file=sys.stderr)
        return 1

    print("[3/3] release manifest + checksums")
    manifest = make_release_manifest(
        installer_path=installer_exe,
        stage_build_manifest=stage_manifest,
        signed=signed,
        signature_subject=subject,
    )
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "SHA256SUMS").write_text(
        f"{sha256_file(installer_exe)}  {installer_exe.name}\n", encoding="utf-8"
    )
    print(f"\n  {installer_exe}")
    print(f"  sha256 {manifest['installer']['sha256']}")  # type: ignore[index]
    print(f"  signed {signed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
