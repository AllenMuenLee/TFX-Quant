r"""Per-user (HKCU) COM self-registration for the Yuanta OCX controls.

**Verified working this session (2026-08-16)**, against the real vendor `.ocx`
files, under a real x32 Python interpreter, with a real network round-trip to the
vendor's test login endpoint — see `docs/adr/0004-broker-session-architecture.md`'s
"Execution attempt findings" for the full trail. Two things this module exists
because of:

1. The standard installer path (`regsvr32`/`DllRegisterServer`, called by the
   vendor's own `install_*.bat` scripts) writes to `HKEY_LOCAL_MACHINE\Software\
   Classes` and genuinely requires administrator rights — it fails with
   `SELFREG_E_CLASS` (0x80040200) without them. Not every operator running this
   desktop app will have (or want to grant) admin rights.
2. Windows COM activation also honors `HKEY_CURRENT_USER\Software\Classes` as a
   registration location — it's merged into the read view every `HKEY_CLASSES_ROOT`
   lookup sees, with **no administrator rights required**. This module writes the
   handful of registry values `DllRegisterServer` would have written
   (`CLSID\{...}`, `CLSID\{...}\ProgID`, `CLSID\{...}\InprocServer32` +
   `ThreadingModel=Apartment`, `CLSID\{...}\Control`, `<ProgID>\CLSID`) directly
   under `HKEY_CURRENT_USER\Software\Classes` instead. This is standard, documented
   per-user COM registration, not a hack — confirmed via `comtypes.client.
   CreateObject(progid)` correctly resolving to the real Yuanta dispatch interface
   (matching type-library IIDs exactly) after this registration, with no admin
   rights, no `regsvr32` call, and no UAC prompt.

Idempotent and safe to call every time the real (non-mock) gateway starts — it only
ever writes these specific keys, never touches HKLM, and never removes anything a
real installer might have set up.
"""

from __future__ import annotations

import os
import winreg

from tfx_quant.infrastructure.yuanta.errors import YuantaSessionError

# Conventional install locations per the vendor's own install_*.bat scripts
# (`交易API元件及說明文件/.../使用說明.txt`: "將API元件資料夾複製到 C:\Yuanta";
# quote: "...置於 C:\Yuanta\QAPI 資料夾中"). This project is x32-only (see ADR 0001),
# so only the 32-bit trading OCX path is used in practice — the 64-bit path constant
# is kept only for documentation/completeness, never selected at runtime.
TRADE_OCX_PATH_32BIT = r"C:\Yuanta\API\YuantaOrd.ocx"
TRADE_OCX_PATH_64BIT = r"C:\Yuanta\API_x64\YuantaOrd64.ocx"
QUOTE_OCX_PATH = r"C:\Yuanta\QAPI\YuantaQuote_v2.1.2.9.ocx"

TRADE_PROGID_32BIT = "Yuanta.YuantaOrdCtrl.1"
TRADE_CLSID_32BIT = "{70AA5E9A-4564-4C29-9403-4407FE8A7358}"
QUOTE_PROGID = "YUANTAQUOTE.YuantaQuoteCtrl.1"
QUOTE_CLSID = "{8E7FB42A-1137-467E-98C6-830C9B02EA82}"


def register_control_per_user(*, progid: str, clsid: str, ocx_path: str) -> None:
    """Writes the same registry values `regsvr32`/`DllRegisterServer` would have,
    under `HKEY_CURRENT_USER\\Software\\Classes` instead of `HKEY_LOCAL_MACHINE`."""
    if not os.path.exists(ocx_path):
        raise YuantaSessionError(
            f"找不到元件檔案 {ocx_path!r}；請依供應商安裝說明將元件複製到此路徑"
            "（見 infrastructure/yuanta/README.md）。"
        )

    root = winreg.HKEY_CURRENT_USER
    base = "Software\\Classes"

    def set_default(path: str, value: str) -> None:
        key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE)
        try:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, value)
        finally:
            winreg.CloseKey(key)

    set_default(f"{base}\\CLSID\\{clsid}", "")
    set_default(f"{base}\\CLSID\\{clsid}\\ProgID", progid)
    set_default(f"{base}\\CLSID\\{clsid}\\InprocServer32", ocx_path)

    key = winreg.OpenKey(root, f"{base}\\CLSID\\{clsid}\\InprocServer32", 0, winreg.KEY_WRITE)
    try:
        winreg.SetValueEx(key, "ThreadingModel", 0, winreg.REG_SZ, "Apartment")
    finally:
        winreg.CloseKey(key)

    set_default(f"{base}\\CLSID\\{clsid}\\Control", "")
    set_default(f"{base}\\{progid}", "")
    set_default(f"{base}\\{progid}\\CLSID", clsid)


def register_all_per_user() -> None:
    """Registers the trading OCX (32-bit — this project is x32-only, see ADR 0001)
    and the quote OCX. Call this once before constructing the real adapters; safe
    and idempotent to call on every startup."""
    register_control_per_user(
        progid=TRADE_PROGID_32BIT, clsid=TRADE_CLSID_32BIT, ocx_path=TRADE_OCX_PATH_32BIT
    )
    register_control_per_user(progid=QUOTE_PROGID, clsid=QUOTE_CLSID, ocx_path=QUOTE_OCX_PATH)
