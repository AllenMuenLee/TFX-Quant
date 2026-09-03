"""Value objects for open-position mark-to-market.

Unrealized P&L is only ever a *separate* line from realized P&L (Feature 11: "未實現…
可…以單獨欄位呈現…絕不可混入已實現數字"). When the mark price cannot be trusted — feed
stale, disconnected, gapped, or invalid — `unrealized_pnl` is `None`, never a
synthesized fill-in number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.timestamp import Timestamp


class PriceQuality(StrEnum):
    OK = "OK"
    STALE = "STALE"
    GAP = "GAP"
    UNAVAILABLE = "UNAVAILABLE"
    """No valid price has been observed for this market yet this run."""


@dataclass(frozen=True, slots=True)
class OpenPositionValuation:
    instrument: Instrument
    contract: ContractMonth
    net_lots: int
    """Signed — positive long, negative short."""
    avg_cost: Decimal
    """Weighted-average entry price per lot, from the broker simulator's actual fills."""
    multiplier: Decimal
    mark_price: Decimal | None
    unrealized_pnl: Decimal | None
    price_quality: PriceQuality
    last_price_at: Timestamp | None


@dataclass(frozen=True, slots=True)
class ValuationSnapshot:
    as_of: Timestamp
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    """`None` when any open position lacks a trustworthy mark."""
    total_pnl: Decimal | None
    """`None` whenever `unrealized_pnl` is `None`."""
    open_positions: tuple[OpenPositionValuation, ...]
    simulation: bool


__all__ = ["OpenPositionValuation", "PriceQuality", "ValuationSnapshot"]
