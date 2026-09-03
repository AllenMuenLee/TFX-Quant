from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tfx_quant.domain.market_data import MarketDataGap, RecordedMarketEvent


class MarketEventRepository(Protocol):
    def append(self, event: RecordedMarketEvent) -> bool:
        """Commit an event. Return False for an identical session/sequence replay."""
        ...

    def list_events(self, symbol: str) -> Sequence[RecordedMarketEvent]: ...

    def record_gap(self, gap: MarketDataGap) -> None: ...

    def list_gaps(self, symbol: str) -> Sequence[MarketDataGap]: ...
