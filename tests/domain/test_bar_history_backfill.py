from __future__ import annotations

from datetime import date

import pytest

from tfx_quant.domain.bar_history_backfill import chunk_consecutive_days


def _d(y: int, m: int, d: int) -> date:
    return date(y, m, d)


def test_chunk_consecutive_days_empty_input() -> None:
    assert chunk_consecutive_days([], max_span_days=5) == []


def test_chunk_consecutive_days_single_day() -> None:
    days = [_d(2026, 9, 16)]
    assert chunk_consecutive_days(days, max_span_days=5) == [(_d(2026, 9, 16), _d(2026, 9, 16))]


def test_chunk_consecutive_days_fits_within_one_span() -> None:
    days = [_d(2026, 9, 16), _d(2026, 9, 17), _d(2026, 9, 18)]
    assert chunk_consecutive_days(days, max_span_days=5) == [(_d(2026, 9, 16), _d(2026, 9, 18))]


def test_chunk_consecutive_days_weekend_gap_does_not_force_a_new_chunk() -> None:
    # Fri 9/18 and Mon 9/21 — a plain calendar-date span still covers both.
    days = [_d(2026, 9, 18), _d(2026, 9, 21)]
    assert chunk_consecutive_days(days, max_span_days=5) == [(_d(2026, 9, 18), _d(2026, 9, 21))]


def test_chunk_consecutive_days_splits_when_span_exceeds_max() -> None:
    days = [_d(2026, 9, 16), _d(2026, 9, 17), _d(2026, 9, 25)]
    assert chunk_consecutive_days(days, max_span_days=5) == [
        (_d(2026, 9, 16), _d(2026, 9, 17)),
        (_d(2026, 9, 25), _d(2026, 9, 25)),
    ]


def test_chunk_consecutive_days_exact_boundary_stays_in_one_chunk() -> None:
    # max_span_days=5 means up to 5 calendar days end-to-end (inclusive), i.e. a
    # 4-day delta between first and last day of the chunk.
    days = [_d(2026, 9, 16), _d(2026, 9, 20)]
    assert chunk_consecutive_days(days, max_span_days=5) == [(_d(2026, 9, 16), _d(2026, 9, 20))]


def test_chunk_consecutive_days_one_day_past_boundary_splits() -> None:
    days = [_d(2026, 9, 16), _d(2026, 9, 21)]
    assert chunk_consecutive_days(days, max_span_days=5) == [
        (_d(2026, 9, 16), _d(2026, 9, 16)),
        (_d(2026, 9, 21), _d(2026, 9, 21)),
    ]


def test_chunk_consecutive_days_rejects_non_positive_max_span() -> None:
    with pytest.raises(ValueError):
        chunk_consecutive_days([_d(2026, 9, 16)], max_span_days=0)
