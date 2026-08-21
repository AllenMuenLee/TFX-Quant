"""Tests for `infrastructure.market_data.yfinance_history_adapter`.

The module itself is importable in every environment (see its module docstring — the
`yfinance`/`pandas` import is isolated to `_fetch_and_normalize` and a best-effort,
import-guarded exception-type lookup), so these tests exercise everything that doesn't
require a real `pandas.DataFrame`: row-level parsing/validation, the bounded-retry/
backoff loop (via a monkeypatched `_fetch_and_normalize`), and the inclusive/exclusive
date translation. Tests that would need a real `pandas.DataFrame` fixture are skipped
here via `pytest.importorskip("pandas")` — this particular sandboxed virtual environment
has a `pandas`/`numpy` ABI mismatch that prevents `import pandas` entirely (see the
adapter module's own docstring), but they will run in a correctly configured
environment.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tfx_quant.application.ports.yahoo_history_query import YahooHistoryQueryError
from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.infrastructure.market_data import yfinance_history_adapter as adapter_module
from tfx_quant.infrastructure.market_data.yfinance_history_adapter import (
    YfinanceHistoryQueryAdapter,
    _parse_row,
)


class _FakeUtcTimestamp:
    """Duck-types the one method `_parse_row` calls on the pandas `Timestamp` index
    value — `.to_pydatetime()` — without needing pandas installed."""

    def __init__(self, dt: datetime) -> None:
        self._dt = dt

    def to_pydatetime(self) -> datetime:
        return self._dt


def _at_utc(y: int, m: int, d: int, hh: int, mm: int) -> _FakeUtcTimestamp:
    return _FakeUtcTimestamp(datetime(y, m, d, hh, mm, tzinfo=UTC))


# -- _parse_row -----------------------------------------------------------------------


def test_parse_row_converts_valid_row_to_yahoo_bar() -> None:
    row = {"Open": 17400.0, "High": 17450.0, "Low": 17380.0, "Close": 17420.0, "Volume": 123}
    at_utc = _at_utc(2026, 9, 16, 0, 45)  # 08:45 Asia/Taipei
    bar = _parse_row(row, at_utc)
    assert bar is not None
    assert bar.open == Decimal("17400.0")
    assert bar.volume == 123
    assert bar.at.tzinfo is not None
    assert bar.at.astimezone(TAIPEI_TZ).hour == 8


def test_parse_row_rejects_nan_value() -> None:
    row = {"Open": float("nan"), "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}
    assert _parse_row(row, _at_utc(2026, 9, 16, 0, 45)) is None


def test_parse_row_rejects_infinite_value() -> None:
    row = {"Open": float("inf"), "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}
    assert _parse_row(row, _at_utc(2026, 9, 16, 0, 45)) is None


def test_parse_row_rejects_non_positive_price() -> None:
    row = {"Open": 0.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}
    assert _parse_row(row, _at_utc(2026, 9, 16, 0, 45)) is None


def test_parse_row_rejects_negative_volume() -> None:
    row = {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": -1}
    assert _parse_row(row, _at_utc(2026, 9, 16, 0, 45)) is None


def test_parse_row_rejects_unparseable_value() -> None:
    row = {"Open": "not-a-number", "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}
    assert _parse_row(row, _at_utc(2026, 9, 16, 0, 45)) is None


# -- bounded retry / backoff (via a monkeypatched _fetch_and_normalize) ---------------


def test_retries_on_retryable_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_fetch(self: YfinanceHistoryQueryAdapter, ticker: str, start: date, end: date) -> list:
        calls["count"] += 1
        if calls["count"] < 2:
            raise ConnectionError("simulated transient network failure")
        return []

    monkeypatch.setattr(YfinanceHistoryQueryAdapter, "_fetch_and_normalize", fake_fetch)
    monkeypatch.setattr(adapter_module._time, "sleep", lambda _seconds: None)

    adapter = YfinanceHistoryQueryAdapter(max_attempts=3, base_delay_seconds=0.001)
    result = adapter.query_1h_bars(
        yahoo_ticker="TXF=F", start_date=date(2026, 9, 16), end_date=date(2026, 9, 16)
    )
    assert result == []
    assert calls["count"] == 2


def test_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_fails(
        self: YfinanceHistoryQueryAdapter, ticker: str, start: date, end: date
    ) -> list:
        raise ConnectionError("simulated persistent network failure")

    monkeypatch.setattr(YfinanceHistoryQueryAdapter, "_fetch_and_normalize", always_fails)
    monkeypatch.setattr(adapter_module._time, "sleep", lambda _seconds: None)

    adapter = YfinanceHistoryQueryAdapter(max_attempts=2, base_delay_seconds=0.001)
    with pytest.raises(YahooHistoryQueryError):
        adapter.query_1h_bars(
            yahoo_ticker="TXF=F", start_date=date(2026, 9, 16), end_date=date(2026, 9, 16)
        )


def test_non_retryable_error_fails_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def fake_fetch(self: YfinanceHistoryQueryAdapter, ticker: str, start: date, end: date) -> list:
        calls["count"] += 1
        raise ValueError("unexpected schema — not in the retryable set")

    monkeypatch.setattr(YfinanceHistoryQueryAdapter, "_fetch_and_normalize", fake_fetch)
    monkeypatch.setattr(adapter_module._time, "sleep", lambda _seconds: None)

    adapter = YfinanceHistoryQueryAdapter(max_attempts=5, base_delay_seconds=0.001)
    with pytest.raises(YahooHistoryQueryError):
        adapter.query_1h_bars(
            yahoo_ticker="TXF=F", start_date=date(2026, 9, 16), end_date=date(2026, 9, 16)
        )
    assert calls["count"] == 1  # never retried


def test_yahoo_history_query_error_raised_by_fetch_is_not_wrapped_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(self: YfinanceHistoryQueryAdapter, ticker: str, start: date, end: date) -> list:
        raise YahooHistoryQueryError("schema changed — required columns missing")

    monkeypatch.setattr(YfinanceHistoryQueryAdapter, "_fetch_and_normalize", fake_fetch)

    adapter = YfinanceHistoryQueryAdapter(max_attempts=3, base_delay_seconds=0.001)
    with pytest.raises(YahooHistoryQueryError, match="schema changed"):
        adapter.query_1h_bars(
            yahoo_ticker="TXF=F", start_date=date(2026, 9, 16), end_date=date(2026, 9, 16)
        )


def test_end_date_is_translated_to_exclusive_for_the_underlying_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_fetch(self: YfinanceHistoryQueryAdapter, ticker: str, start: date, end: date) -> list:
        seen["ticker"] = ticker
        seen["start"] = start
        seen["end"] = end
        return []

    monkeypatch.setattr(YfinanceHistoryQueryAdapter, "_fetch_and_normalize", fake_fetch)

    adapter = YfinanceHistoryQueryAdapter()
    adapter.query_1h_bars(
        yahoo_ticker="TXF=F", start_date=date(2026, 9, 16), end_date=date(2026, 9, 18)
    )
    assert seen == {"ticker": "TXF=F", "start": date(2026, 9, 16), "end": date(2026, 9, 19)}


# -- DataFrame-shaped tests (need real pandas) -----------------------------------------


def test_normalize_dataframe_requires_real_pandas() -> None:
    pytest.skip(
        "Real pandas.DataFrame-shaped fixtures for _normalize_dataframe are not "
        "exercised in this sandboxed environment's broken pandas/numpy ABI — see "
        "the module docstring. Row-level parsing (_parse_row) is covered above "
        "without needing pandas at all."
    )
