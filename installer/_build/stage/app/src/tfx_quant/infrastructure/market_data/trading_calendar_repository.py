"""JsonTradingCalendarRepository — the controlled 交易日曆 backing store.

Loads `trading_calendar.example.json` (or an operator-supplied path — see
`TradingSettings`) into the `TradingCalendarRepository` port's shape. No vendor API
documents an exchange holiday calendar (see `infrastructure/yuanta/README.md`'s
inventory — nothing resembling one exists in either extracted PDF), so — mirroring
`instrument_master_repository.py`'s precedent (ADR 0005) — this is a version-controlled
JSON file an operator maintains, never a live query or a computed rule. Unlike
`instrument_master_repository.py`, this data is exchange-wide, not Yuanta-specific, so it
lives in the generic `infrastructure.market_data` package rather than
`infrastructure.yuanta`.
"""

from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Any

from tfx_quant.infrastructure.market_data.errors import TradingCalendarFileError


def _parse_date(value: object, *, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise TradingCalendarFileError(
            f"{label} 格式錯誤（需為 YYYY-MM-DD），得到 {value!r}"
        ) from exc


def _parse_time(value: object, *, label: str) -> time:
    try:
        hour_str, minute_str = str(value).split(":", 1)
        return time(int(hour_str), int(minute_str))
    except ValueError as exc:
        raise TradingCalendarFileError(f"{label} 格式錯誤（需為 HH:MM），得到 {value!r}") from exc


def _parse_early_close(raw: dict[str, Any], *, index: int) -> tuple[date, time]:
    if "date" not in raw or "day_session_end" not in raw:
        raise TradingCalendarFileError(
            f"第 {index} 筆 early_closes 缺少 date 或 day_session_end 欄位"
        )
    d = _parse_date(raw["date"], label=f"第 {index} 筆 early_closes.date")
    end = _parse_time(raw["day_session_end"], label=f"第 {index} 筆 early_closes.day_session_end")
    return d, end


class JsonTradingCalendarRepository:
    """Implements `application.ports.trading_calendar.TradingCalendarRepository`."""

    def __init__(self, path: Path) -> None:
        raw_text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise TradingCalendarFileError(f"交易日曆 {path} 不是合法 JSON：{exc}") from exc

        holidays_raw = raw.get("holidays")
        if not isinstance(holidays_raw, list):
            raise TradingCalendarFileError(f"交易日曆 {path} 缺少 holidays 陣列")
        self._holidays = frozenset(
            _parse_date(h, label=f"holidays[{i}]") for i, h in enumerate(holidays_raw)
        )

        early_closes_raw = raw.get("early_closes", [])
        if not isinstance(early_closes_raw, list):
            raise TradingCalendarFileError(f"交易日曆 {path} 的 early_closes 必須是陣列")
        early_closes: dict[date, time] = {}
        for i, entry in enumerate(early_closes_raw):
            d, end = _parse_early_close(entry, index=i)
            if d in early_closes:
                raise TradingCalendarFileError(f"交易日曆 {path} 有重複的 early_closes 日期：{d}")
            early_closes[d] = end
        self._early_closes = early_closes

    def get_holidays(self) -> frozenset[date]:
        return self._holidays

    def get_early_closes(self) -> dict[date, time]:
        return dict(self._early_closes)
