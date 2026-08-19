"""成交 tick — one validated market-data trade push for one instrument/contract.

`serial_no` (SPARK API's `StockTickResult.SerialNo`) is a real, documented per-symbol
trade sequence number — "以股票代碼個別編序號，從1開始" (numbered per stock code,
starting from 1) — see `infrastructure/yuanta/market_data_parsing.py` and
`docs/adr/0006-market-data-and-bar-aggregation.md`'s addendum. This is the ordering/
dedup key `BarAggregator` uses to drop duplicate or late-arriving pushes; it replaces
an earlier, pre-SPARK-API-pivot design that used the legacy OCX API's `TolMatchQty`
(cumulative session volume) as a proxy, since that API documented no real sequence
number.
"""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidTickError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True, slots=True)
class Tick:
    instrument: Instrument
    contract: ContractMonth
    at: Timestamp
    """Already resolved to the correct trading day — see
    `TradingCalendar.resolve_tick_timestamp`. Never a bare exchange time-of-day."""
    price: Price
    size: int
    """This push's traded quantity (`DealVol`)."""
    serial_no: int
    """Per-symbol trade sequence number (`SerialNo`), starting at 1 — the ordering/
    dedup key; see module docstring."""

    def __post_init__(self) -> None:
        if self.size < 0:
            raise InvalidTickError(f"size must be >= 0, got {self.size}")
        if self.serial_no < 1:
            raise InvalidTickError(f"serial_no must be >= 1, got {self.serial_no}")
