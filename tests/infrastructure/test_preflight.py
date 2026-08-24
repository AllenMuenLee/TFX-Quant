from __future__ import annotations

import struct
from pathlib import Path

import pytest

import tfx_quant.infrastructure.yuanta.preflight as preflight
from tfx_quant.infrastructure.yuanta.errors import PreflightCheckFailed


def test_preflight_checks_matching_bitness_component_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    is_64_bit = struct.calcsize("P") * 8 == 64
    library = "YuantaOrdLibX64.dll" if is_64_bit else "YuantaOrdLib.dll"
    ocx = "YuantaOrd64.ocx" if is_64_bit else "YuantaOrd.ocx"
    (tmp_path / library).write_bytes(b"type library")
    (tmp_path / ocx).write_bytes(b"ocx")
    monkeypatch.setattr(preflight, "default_api_directory", lambda: tmp_path)

    checks = preflight.run_preflight_checks()

    assert next(check for check in checks if check.name == library).passed
    assert next(check for check in checks if check.name == ocx).passed


def test_preflight_fails_when_matching_component_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(preflight, "default_api_directory", lambda: tmp_path)
    checks = preflight.run_preflight_checks()

    with pytest.raises(PreflightCheckFailed):
        preflight.raise_if_any_failed(checks)


def test_run_preflight_checks_returns_all_four_checks() -> None:
    assert len(preflight.run_preflight_checks()) == 4


def test_raise_if_any_failed_aggregates_failures() -> None:
    checks = [
        preflight.PreflightCheck(name="a", passed=False, message="first"),
        preflight.PreflightCheck(name="b", passed=True, message="ok"),
        preflight.PreflightCheck(name="c", passed=False, message="third"),
    ]
    with pytest.raises(PreflightCheckFailed) as exc_info:
        preflight.raise_if_any_failed(checks)
    assert "first" in str(exc_info.value)
    assert "third" in str(exc_info.value)
    assert "ok" not in str(exc_info.value)


def test_raise_if_any_failed_accepts_all_passed() -> None:
    preflight.raise_if_any_failed([preflight.PreflightCheck(name="x", passed=True, message="ok")])
