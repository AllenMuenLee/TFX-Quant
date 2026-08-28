"""Immutable execution ledger and realized-P&L report value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp


class PositionEffect(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    AUTO = "AUTO"


@dataclass(frozen=True, slots=True)
class LedgerFill:
    fill_id: str
    broker_order_no: str
    order_correlation: str
    masked_account: str
    instrument: Instrument
    contract: ContractMonth
    side: Side
    position_effect: PositionEffect
    quantity: int
    price: Decimal
    filled_at: Timestamp
    trading_day: date
    commission: Decimal | None
    tax: Decimal | None
    source: str

    def __post_init__(self) -> None:
        if not self.fill_id.strip() or not self.broker_order_no.strip() or not self.source.strip():
            raise ValueError("fill_id, broker_order_no, and source must not be empty")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        for name, value in (
            ("price", self.price),
            ("commission", self.commission),
            ("tax", self.tax),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, Decimal)):
                raise TypeError(f"{name} must be Decimal or None")
        if self.price <= 0:
            raise ValueError("price must be positive")
        if self.commission is not None and self.commission < 0:
            raise ValueError("commission cannot be negative")
        if self.tax is not None and self.tax < 0:
            raise ValueError("tax cannot be negative")
        if "*" not in self.masked_account:
            raise ValueError("masked_account must contain masking characters")

    @property
    def provisional_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.commission is None:
            reasons.append("commission_unknown")
        if self.tax is None:
            reasons.append("tax_unknown")
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class RealizedTrade:
    trading_day: date
    instrument: Instrument
    contract: ContractMonth
    side: Side
    quantity: int
    open_price: Decimal
    close_price: Decimal
    multiplier: Decimal
    gross_pnl: Decimal
    commission: Decimal
    tax: Decimal
    net_pnl: Decimal
    open_fill_ids: tuple[str, ...]
    close_fill_id: str
    matching_method: str = "FIFO"
    matching_version: str = "1"
    provisional_reasons: tuple[str, ...] = ()

    @property
    def provisional(self) -> bool:
        return bool(self.provisional_reasons)


@dataclass(frozen=True, slots=True)
class ReportSummary:
    period: date
    gross_pnl: Decimal
    commission: Decimal
    tax: Decimal
    net_pnl: Decimal
    filled_lots: int
    provisional: bool


@dataclass(frozen=True, slots=True)
class TradeReport:
    generated_at: Timestamp
    timezone: str
    start: date
    end: date
    fills: tuple[LedgerFill, ...]
    realized_trades: tuple[RealizedTrade, ...]
    daily: tuple[ReportSummary, ...]
    monthly: tuple[ReportSummary, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


def money_round(value: Decimal, quantum: Decimal = Decimal("1")) -> Decimal:
    """Round TWD amounts with an explicit, auditable rule."""
    return value.quantize(quantum, rounding=ROUND_HALF_UP)
