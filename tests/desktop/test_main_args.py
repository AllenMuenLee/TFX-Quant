from __future__ import annotations

from pathlib import Path

from tfx_quant.desktop.__main__ import _parse_args


def test_no_argument_uses_the_bundled_example_settings() -> None:
    path = _parse_args([])
    assert path.name == "settings.example.json"


def test_a_settings_path_is_passed_through() -> None:
    path = _parse_args(["C:/tmp/my-settings.json"])
    assert path == Path("C:/tmp/my-settings.json")
