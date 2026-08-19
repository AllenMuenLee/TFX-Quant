"""Startup preflight checks for the real Yuanta SPARK API session.

Per the implementation prompt: "啟動前檢查 API 安裝...版本、憑證與必要權限，回報可操作
的中文錯誤" (before starting, check API installation, version, credentials, and
required permissions — report actionable Chinese errors). Every check here is
independently verifiable and none of it is guessed — see
`docs/adr/0001-python-version-and-runtime.md`/`0004-broker-session-architecture.md`.

The legacy OCX API's bitness/COM-registration checks are gone — SPARK API has no such
constraint (see ADR 0001's addendum): it's a `pythonnet`-hosted .NET 8 component, not a
bitness-locked ActiveX control needing per-user registry registration.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from tfx_quant.infrastructure.yuanta.spark_client import default_dll_directory


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    passed: bool
    message: str
    """Actionable Chinese message. Always populated, even on success (states what was
    verified), so `compute_readiness()`-style UIs can show it directly."""


def _check_pythonnet_importable() -> PreflightCheck:
    try:
        import pythonnet  # noqa: F401
    except ImportError as exc:
        return PreflightCheck(
            name="pythonnet 套件",
            passed=False,
            message=f"缺少 pythonnet 套件，無法呼叫元大 SPARK API 元件：{exc}。"
            "請執行 `pip install pythonnet` 後重新啟動程式。",
        )
    return PreflightCheck(name="pythonnet 套件", passed=True, message="pythonnet 套件可正常匯入。")


def _check_dotnet_sdk_available() -> PreflightCheck:
    """SPARK API's docs require the .NET 8 SDK be installed system-wide (not a pip
    package — see `docs/adr/0001-*.md`'s addendum): "電腦環境請用戶自行安裝 .NET8的
    SDK". `pythonnet`'s `coreclr` loader needs a working .NET runtime to host the CLR
    at all, so this check runs `dotnet --list-runtimes` rather than trying to import
    `clr` here (that import has real side effects — loading the CLR into the process —
    which this preflight check shouldn't trigger just to check availability)."""
    dotnet_path = shutil.which("dotnet")
    if dotnet_path is None:
        return PreflightCheck(
            name=".NET SDK",
            passed=False,
            message="找不到 dotnet 命令列工具，元大 SPARK API 元件需要 .NET 8 SDK 才能"
            "運作。請至 https://dotnet.microsoft.com/download/dotnet/8.0 安裝後重新啟動"
            "程式。",
        )
    try:
        result = subprocess.run(  # noqa: S603
            [dotnet_path, "--list-runtimes"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return PreflightCheck(
            name=".NET SDK", passed=False, message=f"執行 `dotnet --list-runtimes` 失敗：{exc}"
        )
    if "Microsoft.NETCore.App 8." not in result.stdout:
        return PreflightCheck(
            name=".NET SDK",
            passed=False,
            message="已安裝 dotnet 命令列工具，但找不到 .NET 8 執行環境（Microsoft."
            "NETCore.App 8.x）。請安裝 .NET 8 SDK 後重新啟動程式。",
        )
    return PreflightCheck(name=".NET SDK", passed=True, message="已偵測到 .NET 8 執行環境。")


def _check_dll_directory_present() -> PreflightCheck:
    """Checks the conventional local install folder (see `spark_client.
    default_dll_directory`) for `YuantaSparkAPI.dll` — a cheap file-existence check,
    distinct from actually loading it (which `_check_dotnet_sdk_available` and
    constructing a real `SparkApiClient` would do, both real side effects this
    preflight pass avoids)."""
    directory = default_dll_directory()
    dll_path = directory / "YuantaSparkAPI.dll"
    if not dll_path.exists():
        return PreflightCheck(
            name="元大 SPARK API 元件",
            passed=False,
            message=f"找不到元大 SPARK API 元件（{dll_path}）。請依供應商說明下載並解壓縮"
            f"至 {directory}，確保範例程式中所有 .dll 檔案皆已一併複製。",
        )
    return PreflightCheck(name="元大 SPARK API 元件", passed=True, message=f"元件位於 {dll_path}。")


def _check_keyring_importable() -> PreflightCheck:
    """Not a credential check — no credentials exist yet at startup (the operator
    types them into the login screen). This verifies the "安全儲存密碼" checkbox's
    dependency (`keyring`, a declared hard dependency on Windows — see
    `pyproject.toml`) is actually importable."""
    try:
        import keyring  # noqa: F401
    except ImportError as exc:
        return PreflightCheck(
            name="keyring 套件",
            passed=False,
            message=f"缺少 keyring 套件，登入畫面的「安全儲存密碼」功能將無法使用：{exc}。"
            "請執行 `pip install keyring` 後重新啟動程式（其餘登入功能不受影響）。",
        )
    return PreflightCheck(name="keyring 套件", passed=True, message="keyring 套件可正常匯入。")


def run_preflight_checks() -> list[PreflightCheck]:
    """Run every startup check and return all results (not just the first failure) —
    so `compute_readiness()`-style callers can show the operator everything to fix at
    once, per the implementation prompt's "回報可操作的中文錯誤"."""
    return [
        _check_pythonnet_importable(),
        _check_dotnet_sdk_available(),
        _check_dll_directory_present(),
        _check_keyring_importable(),
    ]


def raise_if_any_failed(checks: list[PreflightCheck]) -> None:
    from tfx_quant.infrastructure.yuanta.errors import PreflightCheckFailed

    failed = [c for c in checks if not c.passed]
    if not failed:
        return
    lines = "\n".join(f"- {c.name}：{c.message}" for c in failed)
    raise PreflightCheckFailed(f"啟動前檢查未通過，共 {len(failed)} 項：\n{lines}")
