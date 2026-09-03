from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(script_name: str) -> ModuleType:
    path = _REPO_ROOT / "installer" / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(f"_installer_{script_name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_installer = _load("make_installer")


def test_find_iscc_prefers_valid_explicit_path(tmp_path: Path) -> None:
    exe = tmp_path / "ISCC.exe"
    exe.write_bytes(b"stub")
    assert make_installer.find_iscc(str(exe)) == str(exe)
    assert make_installer.find_iscc(str(tmp_path / "nope.exe")) is None


def test_sign_config_none_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TFX_QUANT_SIGN_THUMBPRINT",
        "TFX_QUANT_SIGN_PFX",
        "TFX_QUANT_SIGN_PFX_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    assert make_installer._sign_config() is None


def test_sign_config_thumbprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFX_QUANT_SIGN_THUMBPRINT", "AA11BB22")
    config = make_installer._sign_config()
    assert config is not None
    assert config["mode"] == "thumbprint"
    assert config["value"] == "AA11BB22"


def test_sign_installer_skips_cleanly_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("TFX_QUANT_SIGN_THUMBPRINT", "TFX_QUANT_SIGN_PFX", "TFX_QUANT_SIGN_PFX_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ")
    assert make_installer.sign_installer(exe) is None
    assert "signing skipped" in capsys.readouterr().out


def test_main_errors_without_a_stage_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = make_installer.main(["--stage-app", str(tmp_path), "--skip-if-no-iscc"])
    assert code == 1
    assert "build-manifest.json" in capsys.readouterr().err


def test_main_skips_when_iscc_absent_but_stage_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "app"
    stage.mkdir()
    (stage / "build-manifest.json").write_text(
        json.dumps({"app_version": "0.1.0", "source_revision": "abc"}), encoding="utf-8"
    )
    monkeypatch.setattr(make_installer, "find_iscc", lambda _explicit: None)
    code = make_installer.main(
        ["--stage-app", str(stage), "--output", str(tmp_path / "out"), "--skip-if-no-iscc"]
    )
    assert code == 0
