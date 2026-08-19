from __future__ import annotations

from datetime import date

import pytest

from tfx_quant.domain.bar_history_backfill import chunk_consecutive_days


def test_empty_input_returns_no_chunks() -> None:
    assert chunk_consecutive_days([], max_span_days=5) == []


def test_single_day_returns_one_chunk() -> None:
    d = date(2026, 8, 3)
    assert chunk_consecutive_days([d], max_span_days=5) == [(d, d)]


def test_days_within_span_merge_into_one_chunk() -> None:
    days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 7)]  # Mon, Tue, Fri
    # span end-to-end is 4 days (Fri - Mon), fits within max_span_days=5
    assert chunk_consecutive_days(days, max_span_days=5) == [(date(2026, 8, 3), date(2026, 8, 7))]


def test_days_beyond_span_split_into_separate_chunks() -> None:
    days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 10)]
    # 8/10 - 8/3 = 7 days, exceeds max_span_days=5, so 8/10 starts a new chunk
    assert chunk_consecutive_days(days, max_span_days=5) == [
        (date(2026, 8, 3), date(2026, 8, 4)),
        (date(2026, 8, 10), date(2026, 8, 10)),
    ]


def test_chunk_span_is_inclusive_of_max_span_days() -> None:
    # exactly max_span_days=5 calendar days apart (start, start+4) must still merge
    start = date(2026, 8, 3)
    end = start.replace(day=7)  # 4 days later -> 5-day inclusive span
    assert chunk_consecutive_days([start, end], max_span_days=5) == [(start, end)]


def test_rejects_non_positive_max_span_days() -> None:
    with pytest.raises(ValueError):
        chunk_consecutive_days([date(2026, 8, 3)], max_span_days=0)


def test_many_scattered_missing_days_across_two_months() -> None:
    days = [
        date(2026, 6, 20),
        date(2026, 6, 21),
        date(2026, 7, 15),
        date(2026, 8, 1),
        date(2026, 8, 2),
        date(2026, 8, 3),
    ]
    chunks = chunk_consecutive_days(days, max_span_days=5)
    assert chunks == [
        (date(2026, 6, 20), date(2026, 6, 21)),
        (date(2026, 7, 15), date(2026, 7, 15)),
        (date(2026, 8, 1), date(2026, 8, 3)),
    ]
