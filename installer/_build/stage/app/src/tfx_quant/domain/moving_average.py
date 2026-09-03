"""20MA — simple-moving-average and slope helpers for the 60-minute strategy engine.

Slope is defined as an exact one-bar Decimal delta between the current closed bar's MA
value and the immediately preceding closed bar's MA value — never an implicit float
`>`/`<` on a possibly-noisy value. Every MA value here is computed from `Price.amount`
Decimals, so this delta is an exact Decimal comparison with no floating-point rounding
involved (see implementation-prompt 05's "不得依畫面角度或浮點數恰好大於／小於零作隱含
判斷" requirement).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum


class MaSlope(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"
    """Equal to the previous value, or not enough history to compare — neither up nor
    down. Entry gates treat this the same as a wrong-direction slope."""


def simple_moving_average(closes: Sequence[Decimal], window: int) -> Decimal | None:
    """The arithmetic mean of the last `window` closes, or `None` if fewer than
    `window` samples are available yet ("樣本不足...不得產生新進場或加碼訊號")."""
    if len(closes) < window:
        return None
    relevant = closes[-window:]
    return sum(relevant, start=Decimal(0)) / window


def moving_average_series(closes: Sequence[Decimal], window: int, count: int) -> list[Decimal]:
    """Up to `count` most-recent simple-moving-average values, oldest first, each
    computed over `window` consecutive closes ending at that point. Shorter than
    `count` while fewer than `window + count - 1` closes have been seen."""
    values: list[Decimal] = []
    n = len(closes)
    for k in range(count):
        end = n - k
        if end < window:
            break
        ma = simple_moving_average(closes[:end], window)
        assert ma is not None
        values.append(ma)
    values.reverse()
    return values


def determine_slope(current: Decimal | None, previous: Decimal | None) -> MaSlope:
    """Exact Decimal comparison between the current bar's 20MA and the immediately
    preceding bar's 20MA. `None` (either value missing) is `NONE`, same as an exact tie."""
    if current is None or previous is None:
        return MaSlope.NONE
    if current > previous:
        return MaSlope.UP
    if current < previous:
        return MaSlope.DOWN
    return MaSlope.NONE


def recent_range(values: Sequence[Decimal]) -> Decimal | None:
    """`max - min` over `values`, or `None` if empty."""
    if not values:
        return None
    return max(values) - min(values)


def is_choppy(values: Sequence[Decimal], *, lookback: int, threshold: Decimal) -> bool:
    """True ("均線走平不交易") when the most recent `lookback` MA values span less than
    `threshold` points. Fewer than `lookback` samples is never choppy by this function
    alone — callers gate new entries on sample sufficiency separately. The boundary
    (`range == threshold`) is deliberately NOT choppy — locked by a fixed test."""
    if len(values) < lookback:
        return False
    span = recent_range(values[-lookback:])
    assert span is not None
    return span < threshold


__all__ = [
    "MaSlope",
    "determine_slope",
    "is_choppy",
    "moving_average_series",
    "recent_range",
    "simple_moving_average",
]
