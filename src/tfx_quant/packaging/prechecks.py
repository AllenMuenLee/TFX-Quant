"""Read-only environment checks for the installer (and re-usable elsewhere).

Every check returns a :class:`PrecheckResult` with a :class:`PrecheckSeverity`:

* ``BLOCK`` — installation cannot safely proceed (wrong OS, no disk space).
* ``WARN`` — the operator should know, but it is not fatal. The Yuanta 交易/行情
  API packages are ``WARN`` on purpose: they are installed separately by the
  client (they need Administrator to register their OCX) and are only required to
  trade in 正式環境 — the app still installs, builds bars, and runs 測試環境
  without them.
* ``INFO`` — recorded for the debug log only.

Nothing here mutates the system, registers a component, or touches a credential.
"""

from __future__ import annotations

import shutil
import struct
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_DEFAULT_MIN_FREE_BYTES = 500 * 1024 * 1024
_MIN_WINDOWS_MAJOR = 10


class PrecheckSeverity(StrEnum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    name: str
    passed: bool
    severity: PrecheckSeverity
    message: str


def _ok(
    name: str, message: str, severity: PrecheckSeverity = PrecheckSeverity.INFO
) -> PrecheckResult:
    return PrecheckResult(name=name, passed=True, severity=severity, message=message)


def _fail(name: str, message: str, severity: PrecheckSeverity) -> PrecheckResult:
    return PrecheckResult(name=name, passed=False, severity=severity, message=message)


def windows_version_ok() -> PrecheckResult:
    name = "Windows 版本"
    if sys.platform != "win32":
        return _fail(name, "目前不是 Windows；安裝檔僅支援 Windows 10/11", PrecheckSeverity.WARN)
    version = sys.getwindowsversion()
    if version.major < _MIN_WINDOWS_MAJOR:
        return _fail(
            name,
            f"需要 Windows 10 以上，偵測到主版本 {version.major}",
            PrecheckSeverity.BLOCK,
        )
    return _ok(name, f"Windows {version.major}.{version.minor} build {version.build}")


def interpreter_is_32bit() -> PrecheckResult:
    """The bundled runtime must be 32-bit — the 行情 (quote) OCX has no 64-bit build
    (see ``infrastructure/yuanta/quote_com_host.py``)."""
    name = "Python 位元數"
    bits = struct.calcsize("P") * 8
    if bits != 32:
        return _fail(
            name,
            f"目前 Python 為 {bits} 位元；本系統與元大行情 OCX 僅支援 32 位元",
            PrecheckSeverity.BLOCK,
        )
    return _ok(name, "32 位元")


def free_disk_ok(path: Path, *, min_bytes: int = _DEFAULT_MIN_FREE_BYTES) -> PrecheckResult:
    name = "磁碟空間"
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        return _fail(name, f"無法讀取磁碟空間：{exc}", PrecheckSeverity.WARN)
    if free < min_bytes:
        return _fail(
            name,
            f"可用空間 {free // (1024 * 1024)} MB，低於需求 {min_bytes // (1024 * 1024)} MB",
            PrecheckSeverity.BLOCK,
        )
    return _ok(name, f"可用空間 {free // (1024 * 1024)} MB")


def _registry_value(root: str, subkey: str, value_name: str) -> object | None:
    if sys.platform != "win32":
        return None
    import winreg

    root_key = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
    }[root]
    for access in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root_key, subkey, 0, access) as handle:
                return winreg.QueryValueEx(handle, value_name)[0]
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return None


def _progid_registered(progid: str) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID"):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def vc_redist_x86_present() -> PrecheckResult:
    """The vendor OCX packages document ``vcredist_x86.exe`` as a prerequisite; the
    embedded interpreter's C extensions want the same runtime."""
    name = "Visual C++ x86 執行環境"
    if sys.platform != "win32":
        return _fail(name, "非 Windows，略過", PrecheckSeverity.INFO)
    installed = _registry_value(
        "HKLM", r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x86", "Installed"
    )
    if installed in (1, "1"):
        return _ok(name, "已安裝")
    return _fail(
        name,
        "找不到 Microsoft Visual C++ 2015-2022 (x86) 可轉散發套件；"
        "請安裝 vcredist_x86.exe（元大 API 資料夾內或 Microsoft 官網）",
        PrecheckSeverity.WARN,
    )


def yuanta_trade_api_status() -> PrecheckResult:
    name = "元大交易 API"
    try:
        from tfx_quant.infrastructure.yuanta.legacy_ocx_host import (
            default_api_directory,
            is_control_registered,
            yuanta_control_progid,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(name, f"無法載入檢查模組：{exc}", PrecheckSeverity.WARN)

    directory = default_api_directory()
    ocx = directory / ("YuantaOrd64.ocx" if struct.calcsize("P") * 8 == 64 else "YuantaOrd.ocx")
    if not ocx.is_file():
        return _fail(
            name,
            f"找不到 {ocx.name}（預期於 {directory}）；正式環境交易前需先安裝元大交易 API"
            "並以系統管理員執行 install_YTFutOrdAP.bat",
            PrecheckSeverity.WARN,
        )
    if not is_control_registered():
        return _fail(
            name,
            f"{yuanta_control_progid()} 尚未註冊；請以系統管理員執行"
            f" {directory / 'install_YTFutOrdAP.bat'} 後重新啟動",
            PrecheckSeverity.WARN,
        )
    return _ok(name, f"已安裝並註冊（{directory}）", PrecheckSeverity.INFO)


def yuanta_quote_api_status() -> PrecheckResult:
    name = "元大行情 API"
    try:
        from tfx_quant.infrastructure.yuanta.quote_com_host import default_quote_api_directory
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(name, f"無法載入檢查模組：{exc}", PrecheckSeverity.WARN)

    directory = default_quote_api_directory()
    ocx = directory / "YuantaQuote_v2.1.2.9.ocx"
    if not ocx.is_file():
        return _fail(
            name,
            f"找不到 {ocx.name}（預期於 {directory}）；請安裝元大行情 API"
            "並以系統管理員執行 install_ytocx.bat",
            PrecheckSeverity.WARN,
        )
    if not _progid_registered("YUANTAQUOTE.YuantaQuoteCtrl.1"):
        return _fail(
            name,
            "YUANTAQUOTE.YuantaQuoteCtrl.1 尚未註冊；請以系統管理員執行"
            f" {directory / 'install_ytocx.bat'} 後重新啟動",
            PrecheckSeverity.WARN,
        )
    return _ok(name, f"已安裝並註冊（{directory}）", PrecheckSeverity.INFO)


def run_all_prechecks(
    *,
    install_dir: Path | None = None,
    min_free_bytes: int = _DEFAULT_MIN_FREE_BYTES,
) -> list[PrecheckResult]:
    target = install_dir or Path.cwd()
    return [
        windows_version_ok(),
        interpreter_is_32bit(),
        free_disk_ok(target, min_bytes=min_free_bytes),
        vc_redist_x86_present(),
        yuanta_trade_api_status(),
        yuanta_quote_api_status(),
    ]


def blocking_failures(results: list[PrecheckResult]) -> list[PrecheckResult]:
    return [r for r in results if not r.passed and r.severity is PrecheckSeverity.BLOCK]


def main(argv: list[str] | None = None) -> int:
    """``python -m tfx_quant.packaging.prechecks`` — prints one line per check and
    exits non-zero if any ``BLOCK`` check failed. The Inno Setup script parses this."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="tfx_quant.packaging.prechecks")
    parser.add_argument("--install-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log", type=Path, default=None, help="append results to an install log")
    args = parser.parse_args(argv)

    results = run_all_prechecks(install_dir=args.install_dir)
    if args.log is not None:
        from tfx_quant.packaging.install_log import append_event

        for r in results:
            append_event(
                args.log,
                "precheck",
                {
                    "name": r.name,
                    "passed": r.passed,
                    "severity": r.severity.value,
                    "message": r.message,
                },
            )
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": r.name,
                        "passed": r.passed,
                        "severity": r.severity.value,
                        "message": r.message,
                    }
                    for r in results
                ],
                ensure_ascii=False,
            )
        )
    else:
        for r in results:
            status = "OK  " if r.passed else f"{r.severity.value:<5}"
            print(f"[{status}] {r.name}: {r.message}")
    return 1 if blocking_failures(results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
