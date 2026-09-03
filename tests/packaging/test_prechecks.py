from __future__ import annotations

from pathlib import Path

import pytest

from tfx_quant.packaging import prechecks
from tfx_quant.packaging.prechecks import (
    PrecheckSeverity,
    blocking_failures,
    free_disk_ok,
    interpreter_is_32bit,
    run_all_prechecks,
    yuanta_quote_api_status,
    yuanta_trade_api_status,
)


def test_interpreter_bitness_check_matches_this_runtime() -> None:
    result = interpreter_is_32bit()
    # The project's supported interpreter is 32-bit; a 64-bit runtime is a hard block.
    if result.passed:
        assert "32" in result.message
    else:
        assert result.severity is PrecheckSeverity.BLOCK


def test_free_disk_ok_passes_for_a_real_directory(tmp_path: Path) -> None:
    result = free_disk_ok(tmp_path, min_bytes=1)
    assert result.passed
    assert result.severity is PrecheckSeverity.INFO


def test_free_disk_ok_blocks_when_requirement_is_absurd(tmp_path: Path) -> None:
    result = free_disk_ok(tmp_path, min_bytes=10**18)
    assert not result.passed
    assert result.severity is PrecheckSeverity.BLOCK


def test_free_disk_ok_walks_up_to_an_existing_parent(tmp_path: Path) -> None:
    missing = tmp_path / "does" / "not" / "exist"
    result = free_disk_ok(missing, min_bytes=1)
    assert result.passed


def test_yuanta_trade_api_missing_is_a_warning_not_a_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tfx_quant.infrastructure.yuanta import legacy_ocx_host

    monkeypatch.setattr(legacy_ocx_host, "default_api_directory", lambda: tmp_path)
    result = yuanta_trade_api_status()
    assert not result.passed
    assert result.severity is PrecheckSeverity.WARN
    assert "YuantaOrd" in result.message


def test_yuanta_trade_api_present_but_unregistered_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tfx_quant.infrastructure.yuanta import legacy_ocx_host

    (tmp_path / "YuantaOrd.ocx").write_bytes(b"stub")
    (tmp_path / "YuantaOrd64.ocx").write_bytes(b"stub")
    monkeypatch.setattr(legacy_ocx_host, "default_api_directory", lambda: tmp_path)
    monkeypatch.setattr(legacy_ocx_host, "is_control_registered", lambda: False)
    result = yuanta_trade_api_status()
    assert not result.passed
    assert result.severity is PrecheckSeverity.WARN
    assert "註冊" in result.message


def test_yuanta_trade_api_present_and_registered_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tfx_quant.infrastructure.yuanta import legacy_ocx_host

    (tmp_path / "YuantaOrd.ocx").write_bytes(b"stub")
    (tmp_path / "YuantaOrd64.ocx").write_bytes(b"stub")
    monkeypatch.setattr(legacy_ocx_host, "default_api_directory", lambda: tmp_path)
    monkeypatch.setattr(legacy_ocx_host, "is_control_registered", lambda: True)
    result = yuanta_trade_api_status()
    assert result.passed


def test_yuanta_quote_api_missing_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tfx_quant.infrastructure.yuanta import quote_com_host

    monkeypatch.setattr(quote_com_host, "default_quote_api_directory", lambda: tmp_path)
    result = yuanta_quote_api_status()
    assert not result.passed
    assert result.severity is PrecheckSeverity.WARN


def test_run_all_prechecks_returns_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prechecks, "yuanta_trade_api_status", lambda: prechecks._ok("t", "ok"))
    monkeypatch.setattr(prechecks, "yuanta_quote_api_status", lambda: prechecks._ok("q", "ok"))
    results = run_all_prechecks()
    assert len(results) == 6
    names = {r.name for r in results}
    assert "磁碟空間" in names and "Python 位元數" in names


def test_blocking_failures_only_returns_blocks() -> None:
    results = [
        prechecks._ok("a", "fine"),
        prechecks._fail("b", "warn", PrecheckSeverity.WARN),
        prechecks._fail("c", "block", PrecheckSeverity.BLOCK),
    ]
    blocks = blocking_failures(results)
    assert [r.name for r in blocks] == ["c"]


def test_prechecks_cli_json_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(prechecks, "yuanta_trade_api_status", lambda: prechecks._ok("t", "ok"))
    monkeypatch.setattr(prechecks, "yuanta_quote_api_status", lambda: prechecks._ok("q", "ok"))
    code = prechecks.main(["--json"])
    out = capsys.readouterr().out
    assert '"severity"' in out
    assert code in (0, 1)
