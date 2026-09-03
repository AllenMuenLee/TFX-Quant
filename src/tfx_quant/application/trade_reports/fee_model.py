"""The versioned cost model applied to a *simulated* fill.

Only the local broker simulator (測試環境) consults this — a real
Yuanta fill carries `provisional` costs until the broker's own fee data is confirmed. A
`None` field means that cost is unknown, so the resulting `LedgerFill` is flagged
`provisional` rather than being priced at zero (Feature 11: "費用未知…標記 provisional,
不得靜默顯示為最終值").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tfx_quant.application.settings.trading_settings import SimulationFeeModelSettings
from tfx_quant.domain.side import Side


@dataclass(frozen=True, slots=True)
class FillFeeModel:
    version: str
    commission_per_lot: Decimal | None = None
    tax_rate: Decimal | None = None
    slippage_ticks: Decimal | None = None

    def commission(self, *, lots: int) -> Decimal | None:
        """Total commission for a `lots`-lot fill, or `None` when the schedule is
        unknown."""
        if self.commission_per_lot is None:
            return None
        return self.commission_per_lot * lots

    def tax(self, *, lots: int, price: Decimal, multiplier: Decimal) -> Decimal | None:
        """TAIFEX 期交稅 on the contract notional (`price × multiplier × lots`), or `None`
        when the rate is unknown. Not rounded here — the report applies its own audited
        rounding rule."""
        if self.tax_rate is None:
            return None
        return price * multiplier * lots * self.tax_rate

    def apply_slippage(
        self, *, side: Side, reference_price: Decimal, tick_size: Decimal
    ) -> Decimal:
        """The reference price moved `slippage_ticks` against `side`. Used by the broker
        simulator when it generates a fill — never by the ledger, which records a fill's
        price as received."""
        if self.slippage_ticks is None or self.slippage_ticks == 0:
            return reference_price
        offset = self.slippage_ticks * tick_size
        return reference_price + offset if side is Side.BUY else reference_price - offset


PROVISIONAL_FEE_MODEL = FillFeeModel(version="unknown-0")
"""The production / unconfigured default — every simulated fill's costs land unknown
(`provisional`) until a real `simulation_fee_model` block is supplied."""


def fee_model_from_settings(settings: SimulationFeeModelSettings | None) -> FillFeeModel:
    if settings is None:
        return PROVISIONAL_FEE_MODEL
    return FillFeeModel(
        version=settings.version,
        commission_per_lot=settings.commission_per_lot,
        tax_rate=settings.tax_rate,
        slippage_ticks=settings.slippage_ticks,
    )


__all__ = ["FillFeeModel", "PROVISIONAL_FEE_MODEL", "fee_model_from_settings"]
