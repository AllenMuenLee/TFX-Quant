"""委託 — an order request, carrying an idempotency key independent of any broker order number."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidOrderError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side


class OrderKind(StrEnum):
    OPEN = "OPEN"
    """開倉"""
    CLOSE = "CLOSE"
    """平倉"""


class TimeInForce(StrEnum):
    ROD = "ROD"
    FOK = "FOK"
    IOC = "IOC"


@dataclass(frozen=True, slots=True)
class ClientOrderId:
    """An idempotency key for one order submission, independent of the broker's order number.

    Never auto-regenerated on retry — a resend of the same logical order must reuse the
    same ClientOrderId so the broker/adapter layer can detect duplicates.
    """

    value: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID):
            raise InvalidOrderError(f"value must be a UUID, got {type(self.value).__name__}")


@dataclass(frozen=True, slots=True)
class Order:
    client_order_id: ClientOrderId
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    side: Side
    quantity: Quantity
    price: Price
    kind: OrderKind
    time_in_force: TimeInForce
