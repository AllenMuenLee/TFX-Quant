"""Read-only startup checks for the official Yuanta futures order OCX."""

from __future__ import annotations

import platform
import struct
import sys
from dataclasses import dataclass

from tfx_quant.infrastructure.yuanta.legacy_ocx_host import default_api_directory
from tfx_quant.telemetry import get_logger, log_error, log_info

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    passed: bool
    message: str


def run_preflight_checks() -> list[PreflightCheck]:
    directory = default_api_directory()
    is_64_bit = struct.calcsize("P") * 8 == 64
    library_name = "YuantaOrdLibX64.dll" if is_64_bit else "YuantaOrdLib.dll"
    ocx_name = "YuantaOrd64.ocx" if is_64_bit else "YuantaOrd.ocx"
    checks = [
        PreflightCheck("Windows", sys.platform == "win32", "YuantaOrd OCX 僅支援 Windows"),
        PreflightCheck(
            "Python 位元數",
            True,
            f"目前 Python 為 {platform.architecture()[0]}，將載入相同位元數元件",
        ),
        PreflightCheck(
            library_name,
            (directory / library_name).is_file(),
            f"預期路徑：{directory / library_name}",
        ),
        PreflightCheck(
            ocx_name,
            (directory / ocx_name).is_file(),
            f"預期路徑：{directory / ocx_name}；並須以系統管理員執行安裝 bat",
        ),
    ]
    for check in checks:
        log_info(
            _logger,
            "readiness_check_completed",
            check_name=check.name,
            passed=check.passed,
            message=check.message,
        )
    return checks


def raise_if_any_failed(checks: list[PreflightCheck]) -> None:
    from tfx_quant.infrastructure.yuanta.errors import PreflightCheckFailed

    failed = [check for check in checks if not check.passed]
    if not failed:
        return
    lines = "\n".join(f"- {check.name}：{check.message}" for check in failed)
    log_error(_logger, "preflight_checks_failed", failed_count=len(failed))
    raise PreflightCheckFailed(f"元大交易 API 啟動檢查失敗：\n{lines}")


__all__ = ["PreflightCheck", "raise_if_any_failed", "run_preflight_checks"]
