from __future__ import annotations

from decimal import Decimal

from tfx_quant.domain.moving_average import (
    MaSlope,
    determine_slope,
    is_choppy,
    moving_average_series,
    recent_range,
    simple_moving_average,
)


def test_simple_moving_average_none_when_insufficient_samples() -> None:
    closes = [Decimal(i) for i in range(10)]
    assert simple_moving_average(closes, window=35) is None


def test_simple_moving_average_exact_mean_of_last_window() -> None:
    closes = [Decimal(1), Decimal(2), Decimal(3), Decimal(4), Decimal(5)]
    assert simple_moving_average(closes, window=3) == Decimal(4)  # mean(3,4,5)


def test_moving_average_series_returns_up_to_count_values() -> None:
    closes = [Decimal(i) for i in range(1, 40)]  # 1..39
    series = moving_average_series(closes, window=35, count=5)
    assert len(series) == 5
    # window over closes[:39] (last 35 of 1..39) = mean(5..39) = 22
    assert series[-1] == Decimal(22)
    # each earlier value is exactly one less (arithmetic sequence, step 1)
    assert series == [Decimal(18), Decimal(19), Decimal(20), Decimal(21), Decimal(22)]


def test_moving_average_series_shorter_than_count_when_history_thin() -> None:
    closes = [Decimal(i) for i in range(1, 37)]  # 36 samples
    series = moving_average_series(closes, window=35, count=5)
    assert len(series) == 2  # only positions 35 and 36 have a full window


def test_determine_slope_exact_decimal_comparison() -> None:
    assert determine_slope(Decimal("10.01"), Decimal("10.00")) is MaSlope.UP
    assert determine_slope(Decimal("10.00"), Decimal("10.01")) is MaSlope.DOWN
    assert determine_slope(Decimal("10.00"), Decimal("10.00")) is MaSlope.NONE
    assert determine_slope(None, Decimal("10.00")) is MaSlope.NONE
    assert determine_slope(Decimal("10.00"), None) is MaSlope.NONE


def test_recent_range_max_minus_min() -> None:
    values = [Decimal(1), Decimal(5), Decimal(3)]
    assert recent_range(values) == Decimal(4)
    assert recent_range([]) is None


def test_is_choppy_boundary_equal_threshold_is_not_choppy() -> None:
    values = [Decimal(0), Decimal(10)]  # range exactly 10
    assert is_choppy(values, lookback=2, threshold=Decimal(10)) is False


def test_is_choppy_below_threshold_is_choppy() -> None:
    values = [Decimal(0), Decimal("9.99")]
    assert is_choppy(values, lookback=2, threshold=Decimal(10)) is True


def test_is_choppy_false_when_insufficient_lookback_samples() -> None:
    values = [Decimal(0)]
    assert is_choppy(values, lookback=5, threshold=Decimal(10)) is False
