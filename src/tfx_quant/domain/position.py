"""實際持倉 — the broker-confirmed actual position, as of a point in time."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidPositionError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True, slots=True)
class Position:
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    net: NetPosition
    average_price: Price | None
    """None only when `net.is_flat` is True."""
    as_of: Timestamp

    def __post_init__(self) -> None:
        if self.net.is_flat and self.average_price is not None:
            raise InvalidPositionError("average_price must be None for a flat position")
        if not self.net.is_flat and self.average_price is None:
            raise InvalidPositionError("average_price is required for a non-flat position")
