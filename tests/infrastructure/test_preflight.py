from __future__ import annotations

import pytest

import tfx_quant.infrastructure.yuanta.preflight as preflight
from tfx_quant.infrastructure.yuanta.errors import PreflightCheckFailed


def test_pythonnet_importable_check_passes() -> None:
    checks = preflight.run_preflight_checks()
    check = next(c for c in checks if c.name == "pythonnet 套件")
    assert check.passed is True


def test_keyring_importable_check_passes() -> None:
    checks = preflight.run_preflight_checks()
    check = next(c for c in checks if c.name == "keyring 套件")
    assert check.passed is True


def test_dotnet_sdk_check_fails_when_dotnet_not_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    check = preflight._check_dotnet_sdk_available()
    assert check.passed is False
    assert ".NET 8" in check.message


def test_dotnet_sdk_check_fails_when_net8_runtime_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "dotnet")

    class _FakeResult:
        stdout = "Microsoft.NETCore.App 6.0.1 [C:\\Program Files\\dotnet\\shared]"

    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: _FakeResult())
    check = preflight._check_dotnet_sdk_available()
    assert check.passed is False


def test_dotnet_sdk_check_passes_when_net8_runtime_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "dotnet")

    class _FakeResult:
        stdout = "Microsoft.NETCore.App 8.0.4 [C:\\Program Files\\dotnet\\shared]"

    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: _FakeResult())
    check = preflight._check_dotnet_sdk_available()
    assert check.passed is True


def test_dll_directory_check_fails_when_dll_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(preflight, "default_dll_directory", lambda: tmp_path)
    check = preflight._check_dll_directory_present()
    assert check.passed is False
    assert "YuantaSparkAPI.dll" in check.message


def test_dll_directory_check_passes_when_dll_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    (tmp_path / "YuantaSparkAPI.dll").write_bytes(b"not a real dll")  # type: ignore[operator]
    monkeypatch.setattr(preflight, "default_dll_directory", lambda: tmp_path)
    check = preflight._check_dll_directory_present()
    assert check.passed is True


def test_run_preflight_checks_returns_all_four_checks() -> None:
    checks = preflight.run_preflight_checks()
    assert len(checks) == 4


def test_raise_if_any_failed_aggregates_every_failure_not_just_the_first() -> None:
    checks = [
        preflight.PreflightCheck(name="檢查一", passed=False, message="原因一"),
        preflight.PreflightCheck(name="檢查二", passed=True, message="通過"),
        preflight.PreflightCheck(name="檢查三", passed=False, message="原因三"),
    ]

    with pytest.raises(PreflightCheckFailed) as exc_info:
        preflight.raise_if_any_failed(checks)

    message = str(exc_info.value)
    assert "檢查一" in message
    assert "原因一" in message
    assert "檢查三" in message
    assert "原因三" in message
    assert "檢查二" not in message  # the passing check isn't listed as a failure


def test_raise_if_any_failed_does_not_raise_when_all_passed() -> None:
    all_passed = [preflight.PreflightCheck(name="x", passed=True, message="ok")]
    preflight.raise_if_any_failed(all_passed)  # should not raise
