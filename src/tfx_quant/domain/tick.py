"""成交 tick — one validated market-data trade push for one instrument/contract.

`OnGetMktAll` (the Yuanta quote OCX's price-push event — see
`infrastructure/yuanta/README.md`) documents no explicit per-trade sequence number.
`cumulative_volume` (the vendor's `TolMatchQty`, total traded volume since session open)
is the closest available strictly-monotonic-per-symbol field, so it stands in as the
ordering/dedup key `BarAggregator` uses to drop duplicate or late-arriving pushes — see
`domain/bar_aggregator.py` and `docs/adr/0006-market-data-and-bar-aggregation.md`.
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
    """This push's incremental traded quantity (`MatchQty`)."""
    cumulative_volume: int
    """Total traded volume since session open (`TolMatchQty`) — the ordering/dedup key;
    see module docstring."""

    def __post_init__(self) -> None:
        if self.size < 0:
            raise InvalidTickError(f"size must be >= 0, got {self.size}")
        if self.cumulative_volume < 0:
            raise InvalidTickError(
                f"cumulative_volume must be >= 0, got {self.cumulative_volume}"
            )
