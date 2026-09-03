"""損益 — realized and unrealized profit/loss as of a point in time."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.money import Money
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True, slots=True)
class Pnl:
    realized: Money
    unrealized: Money
    as_of: Timestamp

    @property
    def total(self) -> Money:
        return self.realized + self.unrealized
