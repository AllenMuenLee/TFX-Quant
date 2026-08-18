from __future__ import annotations

import winreg
from pathlib import Path

import pytest

from tfx_quant.infrastructure.yuanta import com_registration
from tfx_quant.infrastructure.yuanta.errors import YuantaSessionError


def test_register_control_per_user_raises_when_ocx_file_missing() -> None:
    with pytest.raises(YuantaSessionError, match="找不到元件檔案"):
        com_registration.register_control_per_user(
            progid="Fake.ProgId.1", clsid="{FAKE-CLSID}", ocx_path=r"C:\does\not\exist.ocx"
        )


def test_register_control_per_user_writes_expected_registry_values(tmp_path: Path) -> None:
    """Real registry write, scoped entirely to a throwaway ProgID/CLSID under
    HKEY_CURRENT_USER\\Software\\Classes — this is exactly the per-user mechanism
    `com_registration.py` exists for (see its module docstring), verified working
    against the real vendor files this session. Cleans up after itself."""
    fake_ocx = tmp_path / "Fake.ocx"
    fake_ocx.write_bytes(b"not a real ocx, just needs to exist")

    progid = "Tfx.Quant.Test.ComRegistration.1"
    clsid = "{12345678-1234-1234-1234-1234567890AB}"
    base = "Software\\Classes"

    try:
        com_registration.register_control_per_user(
            progid=progid, clsid=clsid, ocx_path=str(fake_ocx)
        )

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\CLSID\\{clsid}\\ProgID") as key:
            assert winreg.QueryValueEx(key, "")[0] == progid

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, f"{base}\\CLSID\\{clsid}\\InprocServer32"
        ) as key:
            assert winreg.QueryValueEx(key, "")[0] == str(fake_ocx)
            assert winreg.QueryValueEx(key, "ThreadingModel")[0] == "Apartment"

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{progid}\\CLSID") as key:
            assert winreg.QueryValueEx(key, "")[0] == clsid

        # HKEY_CLASSES_ROOT is a merged read view — confirms COM activation would
        # actually see this registration, not just a raw HKCU write.
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\InprocServer32") as key:
            assert winreg.QueryValueEx(key, "")[0] == str(fake_ocx)
    finally:
        _delete_key_tree(winreg.HKEY_CURRENT_USER, f"{base}\\CLSID\\{clsid}")
        _delete_key_tree(winreg.HKEY_CURRENT_USER, f"{base}\\{progid}")


def _delete_key_tree(root: int, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_key_tree(root, f"{path}\\{subkey_name}")
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def test_register_all_per_user_registers_both_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        com_registration,
        "register_control_per_user",
        lambda *, progid, clsid, ocx_path: calls.append((progid, clsid, ocx_path)),
    )

    com_registration.register_all_per_user()

    assert calls == [
        (
            com_registration.TRADE_PROGID_32BIT,
            com_registration.TRADE_CLSID_32BIT,
            com_registration.TRADE_OCX_PATH_32BIT,
        ),
        (
            com_registration.QUOTE_PROGID,
            com_registration.QUOTE_CLSID,
            com_registration.QUOTE_OCX_PATH,
        ),
    ]
