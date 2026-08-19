from __future__ import annotations

from pathlib import Path

import pytest

from tfx_quant.infrastructure.yuanta import login_preferences


@pytest.fixture(autouse=True)
def _isolated_prefs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def test_load_with_no_file_yet_returns_empty_preferences() -> None:
    prefs = login_preferences.load()

    assert prefs.remembered_user_id is None
    assert prefs.remembered_account_no is None


def test_save_and_load_remembered_user_id_round_trips() -> None:
    login_preferences.save_remembered_user_id("A123456789")

    assert login_preferences.load().remembered_user_id == "A123456789"


def test_save_remembered_user_id_none_clears_it() -> None:
    login_preferences.save_remembered_user_id("A123456789")
    login_preferences.save_remembered_user_id(None)

    assert login_preferences.load().remembered_user_id is None


def test_save_remembered_account_no_round_trips_independently_of_user_id() -> None:
    login_preferences.save_remembered_user_id("A123456789")
    login_preferences.save_remembered_account_no("9808900")

    prefs = login_preferences.load()
    assert prefs.remembered_user_id == "A123456789"
    assert prefs.remembered_account_no == "9808900"


def test_load_tolerates_a_corrupt_prefs_file(tmp_path: Path) -> None:
    prefs_path = tmp_path / "tfx_quant" / "login_prefs.json"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text("{not valid json", encoding="utf-8")

    prefs = login_preferences.load()  # must not raise

    assert prefs.remembered_user_id is None
    assert prefs.remembered_account_no is None


def test_load_tolerates_a_prefs_file_that_is_not_a_json_object(tmp_path: Path) -> None:
    prefs_path = tmp_path / "tfx_quant" / "login_prefs.json"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text("[1, 2, 3]", encoding="utf-8")

    prefs = login_preferences.load()  # must not raise

    assert prefs.remembered_user_id is None
