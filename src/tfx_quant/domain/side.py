"""方向 — buy/sell side of an order or fill."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY
