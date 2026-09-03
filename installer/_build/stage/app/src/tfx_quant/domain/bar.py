"""K棒 — an OHLCV bar for one instrument/contract over one timeframe.

**Label/close-time semantics (Feature 04 — 60-minute bars)**: `start` is the bar's
*label* — its open time — not its close time. A 60-minute bar labeled 08:45 covers
[08:45, 09:45) and closes at 09:45 (`end`). This matters because the implementation
prompt's own worked example ("08:45、09:45 的已收 K...最早 10:45 才可判斷建倉") is only
internally consistent under open-time labeling: the bar labeled 08:45 and the bar
labeled 09:45 are both already closed (at 09:45 and 10:45 respectively) before the entry
gate opens at 10:45. See `docs/adr/0006-market-data-and-bar-aggregation.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import Timestamp


class InvalidBarError(DomainError):
    """Raised when a bar's OHLC values are internally inconsistent."""


class CandleColor(StrEnum):
    """紅K/黑K/十字K — `close == open` (DOJI) must interrupt a consecutive red/black
    streak; see `domain.bar_aggregator.CandleStreakCounter`."""

    RED = "RED"
    """紅K — close > open."""
    BLACK = "BLACK"
    """黑K — close < open."""
    DOJI = "DOJI"
    """十字K — close == open."""


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: Instrument
    contract: ContractMonth
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int
    start: Timestamp
    """The bar's label — its open time. See module docstring."""
    end: Timestamp
    """The bar's close time."""

    def __post_init__(self) -> None:
        if self.volume < 0:
            raise InvalidBarError(f"volume must be >= 0, got {self.volume}")
        if self.high.amount < self.low.amount:
            raise InvalidBarError("high must be >= low")
        if not (self.low.amount <= self.open.amount <= self.high.amount):
            raise InvalidBarError("open must be within [low, high]")
        if not (self.low.amount <= self.close.amount <= self.high.amount):
            raise InvalidBarError("close must be within [low, high]")
        if self.end.value < self.start.value:
            raise InvalidBarError("end must be >= start")

    @property
    def candle_color(self) -> CandleColor:
        if self.close.amount > self.open.amount:
            return CandleColor.RED
        if self.close.amount < self.open.amount:
            return CandleColor.BLACK
        return CandleColor.DOJI
