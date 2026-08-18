"""成交 — a confirmed fill (trade execution) report from the broker."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True, slots=True)
class Fill:
    client_order_id: ClientOrderId
    instrument: Instrument
    side: Side
    quantity: Quantity
    price: Price
    at: Timestamp
