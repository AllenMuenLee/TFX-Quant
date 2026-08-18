from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path

import pytest

from tfx_quant.infrastructure.market_data.errors import TradingCalendarFileError
from tfx_quant.infrastructure.market_data.trading_calendar_repository import (
    JsonTradingCalendarRepository,
)

_EXAMPLE_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "tfx_quant"
    / "infrastructure"
    / "market_data"
    / "trading_calendar.example.json"
)


def _write(tmp_path: Path, content: dict[str, object]) -> Path:
    path = tmp_path / "trading_calendar.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_example_calendar_file_loads_successfully() -> None:
    repo = JsonTradingCalendarRepository(_EXAMPLE_PATH)
    holidays = repo.get_holidays()
    assert date(2026, 1, 1) in holidays
    assert repo.get_early_closes() == {}


def test_holidays_and_early_closes_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "holidays": ["2026-01-01", "2026-02-28"],
            "early_closes": [{"date": "2026-06-19", "day_session_end": "11:15"}],
        },
    )
    repo = JsonTradingCalendarRepository(path)
    assert repo.get_holidays() == frozenset({date(2026, 1, 1), date(2026, 2, 28)})
    assert repo.get_early_closes() == {date(2026, 6, 19): time(11, 15)}


def test_early_closes_defaults_to_empty_when_omitted(tmp_path: Path) -> None:
    path = _write(tmp_path, {"holidays": []})
    repo = JsonTradingCalendarRepository(path)
    assert repo.get_early_closes() == {}


def test_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TradingCalendarFileError):
        JsonTradingCalendarRepository(path)


def test_rejects_missing_holidays_array(tmp_path: Path) -> None:
    path = _write(tmp_path, {})
    with pytest.raises(TradingCalendarFileError):
        JsonTradingCalendarRepository(path)


def test_rejects_malformed_holiday_date(tmp_path: Path) -> None:
    path = _write(tmp_path, {"holidays": ["not-a-date"]})
    with pytest.raises(TradingCalendarFileError):
        JsonTradingCalendarRepository(path)


def test_rejects_malformed_early_close_time(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"holidays": [], "early_closes": [{"date": "2026-06-19", "day_session_end": "bad"}]},
    )
    with pytest.raises(TradingCalendarFileError):
        JsonTradingCalendarRepository(path)


def test_rejects_duplicate_early_close_dates(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "holidays": [],
            "early_closes": [
                {"date": "2026-06-19", "day_session_end": "11:15"},
                {"date": "2026-06-19", "day_session_end": "10:00"},
            ],
        },
    )
    with pytest.raises(TradingCalendarFileError):
        JsonTradingCalendarRepository(path)
