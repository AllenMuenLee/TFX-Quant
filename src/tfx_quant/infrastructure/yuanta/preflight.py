"""Startup preflight checks for the real Yuanta gateways.

Per the implementation prompt: "啟動前檢查 API 安裝、DLL/COM 註冊、位元數、版本、憑證
與必要權限，回報可操作的中文錯誤" (before starting, check API installation, DLL/COM
registration, bitness, version, credentials, and required permissions — report
actionable Chinese errors). Every check here is independently verifiable and none of
it is guessed: ProgIDs and install paths come from
`src/tfx_quant/infrastructure/yuanta/README.md` (extracted directly from the vendor
`.ocx` binaries and install scripts).
"""

from __future__ import annotations

import contextlib
import struct
from dataclasses import dataclass

from tfx_quant.infrastructure.yuanta import com_registration
from tfx_quant.infrastructure.yuanta.credentials import CredentialSource
from tfx_quant.infrastructure.yuanta.errors import (
    CredentialsUnavailableError,
    PreflightCheckFailed,
    YuantaSessionError,
)

TRADE_PROGID_32BIT = com_registration.TRADE_PROGID_32BIT
TRADE_PROGID_64BIT = "Yuanta.YuantaOrdCtrl.64"
QUOTE_PROGID = com_registration.QUOTE_PROGID
"""No 64-bit build is shipped for the quote OCX — see the bitness check below, which
is what actually forces this whole process to run as x32 in production."""


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    passed: bool
    message: str
    """Actionable Chinese message. Always populated, even on success (states what was
    verified), so `compute_readiness()`-style UIs can show it directly."""


def _is_64bit_process() -> bool:
    return struct.calcsize("P") == 8


def _check_bitness() -> PreflightCheck:
    if _is_64bit_process():
        return PreflightCheck(
            name="處理程序位元數",
            passed=False,
            message=(
                "目前以 64 位元 Python 執行，但元大行情 API 僅提供 32 位元元件"
                f"（{QUOTE_PROGID}），無 64 位元版本。請改用 32 位元（x32）Python "
                "直譯器執行本程式（見 docs/adr/0001-python-version-and-runtime.md）。"
            ),
        )
    return PreflightCheck(
        name="處理程序位元數",
        passed=True,
        message="以 32 位元（x32）Python 執行，符合行情 API 需求。",
    )


def _check_comtypes_importable() -> PreflightCheck:
    try:
        import comtypes  # noqa: F401
    except ImportError as exc:
        return PreflightCheck(
            name="comtypes 套件",
            passed=False,
            message=f"缺少 comtypes 套件，無法呼叫元大 COM/ActiveX 元件：{exc}。"
            "請執行 `pip install comtypes` 後重新啟動程式。",
        )
    return PreflightCheck(name="comtypes 套件", passed=True, message="comtypes 套件可正常匯入。")


def progid_to_clsid(progid: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{progid}\\CLSID") as key:
            clsid, _ = winreg.QueryValueEx(key, "")
            return str(clsid)
    except OSError:
        return None


def clsid_inproc_server_path(clsid: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\InprocServer32") as key:
            path, _ = winreg.QueryValueEx(key, "")
            return str(path)
    except OSError:
        return None


def resolve_registered_ocx_path(progid: str) -> str | None:
    """The installed `.ocx` file path for a registered ProgID, or `None` if the
    ProgID isn't registered or the resolved path doesn't exist on disk. Used both by
    the preflight check below and by the real adapters (`trade_ocx_adapter.py`/
    `quote_ocx_adapter.py`) to locate the file `comtypes.client.GetModule()` should
    read its type library from — reusing the registry lookup instead of assuming a
    fixed install path."""
    import os

    clsid = progid_to_clsid(progid)
    if clsid is None:
        return None
    path = clsid_inproc_server_path(clsid)
    if path is None or not os.path.exists(path):
        return None
    return path


def _check_com_registration(progid: str, display_name: str) -> PreflightCheck:
    import os

    try:
        import winreg  # noqa: F401
    except ImportError:
        return PreflightCheck(
            name=f"{display_name} COM 註冊",
            passed=False,
            message="非 Windows 環境，無法檢查 COM 註冊（winreg 模組不存在）。",
        )

    clsid = progid_to_clsid(progid)
    if clsid is None:
        return PreflightCheck(
            name=f"{display_name} COM 註冊",
            passed=False,
            message=(
                f"找不到已註冊的 ProgID {progid!r}。本程式會在啟動時自動於目前使用者"
                "（HKEY_CURRENT_USER）下註冊元件，不需要系統管理員權限；若仍失敗，請確認"
                "元件檔案已依供應商安裝說明複製到慣用路徑"
                "（見 infrastructure/yuanta/com_registration.py 的路徑常數）。"
            ),
        )

    dll_path = clsid_inproc_server_path(clsid)
    if dll_path is None or not os.path.exists(dll_path):
        return PreflightCheck(
            name=f"{display_name} COM 註冊",
            passed=False,
            message=(
                f"ProgID {progid!r} 已註冊（CLSID {clsid}），但找不到對應的元件檔案"
                f"（{dll_path!r}）。元件可能已被移動或移除，請依供應商安裝說明重新複製元件。"
            ),
        )

    return PreflightCheck(
        name=f"{display_name} COM 註冊",
        passed=True,
        message=f"ProgID {progid!r} 已註冊，元件位於 {dll_path}。",
    )


def _check_credentials(credential_source: CredentialSource) -> PreflightCheck:
    try:
        credential_source.load()
    except CredentialsUnavailableError as exc:
        return PreflightCheck(name="登入憑證", passed=False, message=str(exc))
    return PreflightCheck(
        name="登入憑證", passed=True, message="已成功讀取登入 ID 與密碼設定來源。"
    )


def run_preflight_checks(credential_source: CredentialSource) -> list[PreflightCheck]:
    """Run every startup check and return all results (not just the first failure) —
    so `compute_readiness()`-style callers can show the operator everything to fix
    at once, per the implementation prompt's "回報可操作的中文錯誤".

    Attempts per-user COM self-registration first (best-effort — see
    `com_registration.py`; requires no admin rights, verified working this session).
    If it fails (e.g. the component files aren't present yet), the COM-registration
    checks below simply report that as a normal failure rather than raising here.
    """
    with contextlib.suppress(YuantaSessionError):
        com_registration.register_all_per_user()

    trade_progid = TRADE_PROGID_64BIT if _is_64bit_process() else TRADE_PROGID_32BIT
    return [
        _check_bitness(),
        _check_comtypes_importable(),
        _check_com_registration(trade_progid, "元大下單 API"),
        _check_com_registration(QUOTE_PROGID, "元大行情 API"),
        _check_credentials(credential_source),
    ]


def raise_if_any_failed(checks: list[PreflightCheck]) -> None:
    failed = [c for c in checks if not c.passed]
    if not failed:
        return
    lines = "\n".join(f"- {c.name}：{c.message}" for c in failed)
    raise PreflightCheckFailed(f"啟動前檢查未通過，共 {len(failed)} 項：\n{lines}")
