from __future__ import annotations

import json
from pathlib import Path

import pytest

from tfx_quant.packaging.install_log import (
    InstallLogger,
    append_event,
    mask_path,
    sanitize_fields,
)


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_mask_path_collapses_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\alice\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\Users\alice\AppData\Roaming")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\alice")
    assert mask_path(r"C:\Users\alice\AppData\Local\tfx_quant\logs\x.log") == (
        r"<LOCALAPPDATA>\tfx_quant\logs\x.log"
    )
    assert (
        mask_path(r"C:\Users\alice\Desktop\settings.json") == r"<USERPROFILE>\Desktop\settings.json"
    )


def test_mask_path_scrubs_foreign_user_directory() -> None:
    assert mask_path(r"D:\Users\bob\stuff\a.db") == r"D:\Users\<user>\stuff\a.db"
    assert mask_path("/home/notusers/x") == "/home/notusers/x"


def test_mask_path_leaves_non_paths_untouched() -> None:
    assert mask_path("0.1.0") == "0.1.0"
    assert mask_path("PRODUCTION") == "PRODUCTION"


def test_sanitize_fields_reduces_secrets_to_presence() -> None:
    out = sanitize_fields(
        {
            "password": "hunter2",
            "cert_pfx_password": "s3cret",
            "api_token": "abc",
            "step": "extract",
        }
    )
    assert out == {
        "password_present": True,
        "cert_pfx_password_present": True,
        "api_token_present": True,
        "step": "extract",
    }
    assert "hunter2" not in json.dumps(out)


def test_sanitize_fields_masks_account_numbers() -> None:
    out = sanitize_fields({"account": "F0210001234567", "user_id": "A123456789"})
    assert out["account_masked"] == "**********4567"
    assert out["user_id_masked"] == "******6789"


def test_install_logger_writes_run_banner_and_steps(tmp_path: Path) -> None:
    log_path = tmp_path / "installer.log"
    with InstallLogger(
        phase="installer",
        package_version="0.1.0",
        app_version="0.1.0",
        log_path=log_path,
    ) as logger:
        with logger.step("extract_runtime"):
            pass
        logger.event("dirs_created", data_dir=r"C:\Users\carol\AppData\Local\tfx_quant")

    records = _read_lines(log_path)
    events = [r["event"] for r in records]
    assert events[0] == "run_started"
    assert "step_started" in events and "step_finished" in events
    assert events[-1] == "run_finished"
    finished = next(r for r in records if r["event"] == "step_finished")
    assert finished["exit_code"] == 0
    dirs = next(r for r in records if r["event"] == "dirs_created")
    assert "carol" not in json.dumps(dirs)


def test_install_logger_step_records_failure_exit_code(tmp_path: Path) -> None:
    log_path = tmp_path / "installer.log"
    logger = InstallLogger(
        phase="installer", package_version="x", app_version="x", log_path=log_path
    )
    with pytest.raises(RuntimeError), logger.step("bad"):
        raise RuntimeError("boom")
    logger.close(exit_code=1, rollback_result="reverted")

    records = _read_lines(log_path)
    finished = next(r for r in records if r["event"] == "step_finished")
    assert finished["exit_code"] == 1
    assert finished["error_type"] == "RuntimeError"
    run_finished = records[-1]
    assert run_finished["rollback_result"] == "reverted"


def test_append_event_has_no_banner(tmp_path: Path) -> None:
    log_path = tmp_path / "iss.log"
    append_event(log_path, "precheck_started", {"password": "nope"})
    append_event(log_path, "precheck_finished", {"exit_code": 0})
    records = _read_lines(log_path)
    assert [r["event"] for r in records] == ["precheck_started", "precheck_finished"]
    assert "nope" not in log_path.read_text(encoding="utf-8")
    assert records[0]["password_present"] is True
