"""The single FIFO lot-matching pass shared by realized-P&L reporting and open-position
valuation.

`match_fills` walks the ledger once and returns both the closed `RealizedTrade`s and the
lots still open (with their weighted-average cost), so realized and unrealized P&L are
computed from exactly one code path — the "相同計算路徑" the 測試環境 acceptance criteria
require. Matching method/version is `FIFO`/`1`, stamped on every `RealizedTrade`.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.trade_report import LedgerFill, RealizedTrade, money_round
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)
_D = Decimal("0")

Multipliers = dict[tuple[object, object], Decimal]


@dataclass(slots=True)
class _Lot:
    fill: LedgerFill
    remaining: int
    fee_per_lot: Decimal
    tax_per_lot: Decimal


@dataclass(frozen=True, slots=True)
class OpenLot:
    """One still-open lot (FIFO order preserved). All open lots for a given
    (instrument, contract, account) always share one `side` — a position is flat before
    it can reverse."""

    instrument: Instrument
    contract: ContractMonth
    masked_account: str
    side: Side
    remaining: int
    open_price: Decimal
    fee_per_lot: Decimal
    tax_per_lot: Decimal
    simulation: bool


@dataclass(frozen=True, slots=True)
class FifoResult:
    realized_trades: tuple[RealizedTrade, ...]
    open_lots: tuple[OpenLot, ...]

    @property
    def open_lot_count(self) -> int:
        return sum(lot.remaining for lot in self.open_lots)


def match_fills(fills: tuple[LedgerFill, ...], multipliers: Multipliers) -> FifoResult:
    positions: dict[tuple[object, object, str], deque[_Lot]] = defaultdict(deque)
    trades: list[RealizedTrade] = []
    for fill in sorted(fills, key=lambda f: (f.filled_at.value, f.fill_id)):
        key = (fill.instrument, fill.contract, fill.masked_account)
        multiplier = multipliers.get((fill.instrument, fill.contract))
        if multiplier is None or not isinstance(multiplier, Decimal) or multiplier <= 0:
            raise ValueError(
                f"positive Decimal multiplier required for {fill.instrument}/{fill.contract}"
            )
        queue = positions[key]
        remaining = fill.quantity
        while remaining and queue and queue[0].fill.side is not fill.side:
            opened = queue[0]
            qty = min(remaining, opened.remaining)
            close_fee = (fill.commission or _D) / fill.quantity * qty
            close_tax = (fill.tax or _D) / fill.quantity * qty
            fees = opened.fee_per_lot * qty + close_fee
            taxes = opened.tax_per_lot * qty + close_tax
            direction = Decimal("1") if opened.fill.side is Side.BUY else Decimal("-1")
            gross = money_round((fill.price - opened.fill.price) * direction * multiplier * qty)
            commission = money_round(fees)
            tax = money_round(taxes)
            reasons = tuple(
                dict.fromkeys((*opened.fill.provisional_reasons, *fill.provisional_reasons))
            )
            simulation = opened.fill.simulation and fill.simulation
            mixed = opened.fill.simulation != fill.simulation
            if mixed:
                reasons = (*reasons, "mixed_simulation_fills")
            trade = RealizedTrade(
                fill.trading_day,
                fill.instrument,
                fill.contract,
                opened.fill.side,
                qty,
                opened.fill.price,
                fill.price,
                multiplier,
                gross,
                commission,
                tax,
                gross - commission - tax,
                (opened.fill.fill_id,),
                fill.fill_id,
                provisional_reasons=reasons,
                simulation=simulation,
            )
            trades.append(trade)
            log_info(
                _logger,
                "realized_pnl_match",
                matching_method="FIFO",
                matching_version="1",
                fill_ids=[opened.fill.fill_id, fill.fill_id],
                multiplier=str(multiplier),
                gross_pnl=str(gross),
                commission=str(commission),
                tax=str(tax),
                net_pnl=str(trade.net_pnl),
                decimal_rounding="ROUND_HALF_UP quantum=1",
                provisional_reasons=reasons,
                simulation=simulation,
            )
            opened.remaining -= qty
            remaining -= qty
            if opened.remaining == 0:
                queue.popleft()
        if remaining:
            queue.append(
                _Lot(
                    fill,
                    remaining,
                    (fill.commission or _D) / fill.quantity,
                    (fill.tax or _D) / fill.quantity,
                )
            )
    open_lots = tuple(
        OpenLot(
            instrument=lot.fill.instrument,
            contract=lot.fill.contract,
            masked_account=lot.fill.masked_account,
            side=lot.fill.side,
            remaining=lot.remaining,
            open_price=lot.fill.price,
            fee_per_lot=lot.fee_per_lot,
            tax_per_lot=lot.tax_per_lot,
            simulation=lot.fill.simulation,
        )
        for queue in positions.values()
        for lot in queue
        if lot.remaining > 0
    )
    return FifoResult(realized_trades=tuple(trades), open_lots=open_lots)


__all__ = ["FifoResult", "OpenLot", "match_fills"]
