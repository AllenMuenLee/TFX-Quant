"""Open-position + realized/unrealized/total P&L rows — Feature 12's 持倉 group.

Unrealized and total are `None` (rendered as "—，資料品質：…") whenever the mark cannot be
trusted; the panel must never show a number the feed did not actually support.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tfx_quant.application.trade_reports.position_valuation_service import (
    PositionValuationService,
)
from tfx_quant.domain.valuation import PriceQuality

_QUALITY_ZH = {
    PriceQuality.OK: "即時",
    PriceQuality.STALE: "行情過期",
    PriceQuality.GAP: "行情中斷（缺口）",
    PriceQuality.UNAVAILABLE: "尚無有效報價",
}


@dataclass(frozen=True, slots=True)
class PositionRow:
    instrument: str
    contract: str
    net_lots: int
    avg_cost: Decimal
    mark_price: Decimal | None
    unrealized_pnl: Decimal | None
    price_quality: str
    last_price_at: str | None


@dataclass(frozen=True, slots=True)
class PositionsView:
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    total_pnl: Decimal | None
    as_of: str
    rows: tuple[PositionRow, ...]
    simulation: bool


def build_positions_view(valuation: PositionValuationService) -> PositionsView:
    snap = valuation.snapshot()
    return PositionsView(
        realized_pnl=snap.realized_pnl,
        unrealized_pnl=snap.unrealized_pnl,
        total_pnl=snap.total_pnl,
        as_of=snap.as_of.value.strftime("%Y-%m-%d %H:%M:%S"),
        rows=tuple(
            PositionRow(
                instrument=p.instrument.display_name_zh,
                contract=f"{p.contract.year:04d}-{p.contract.month:02d}",
                net_lots=p.net_lots,
                avg_cost=p.avg_cost,
                mark_price=p.mark_price,
                unrealized_pnl=p.unrealized_pnl,
                price_quality=_QUALITY_ZH.get(p.price_quality, p.price_quality.value),
                last_price_at=(
                    None
                    if p.last_price_at is None
                    else p.last_price_at.value.strftime("%Y-%m-%d %H:%M:%S")
                ),
            )
            for p in snap.open_positions
        ),
        simulation=snap.simulation,
    )


__all__ = ["PositionRow", "PositionsView", "build_positions_view"]
