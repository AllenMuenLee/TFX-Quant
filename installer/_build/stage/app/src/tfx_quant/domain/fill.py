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
    broker_fill_no: str
    """The broker's own unique fill identifier — the dedup key and the "由 fill ID 串回
    原 intent" traceability key every fill-related log record must carry alongside the
    local order id."""
    broker_seq_no: int
    """Feeds the same per-order monotonic ordering/dedup gate as `OrderReport.
    broker_seq_no` (see `domain/order_state_machine.py`) — order reports and fills for one
    order share a single sequence space."""
