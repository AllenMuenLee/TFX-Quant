"""Persist-first live quote processing pipeline."""

from __future__ import annotations

from collections.abc import Callable

from tfx_quant.application.market_data.event_parser import MarketEventParser
from tfx_quant.application.market_data.realtime_bar_aggregator import (
    ClosedAggregation,
    RealtimeBarAggregator,
)
from tfx_quant.application.ports.market_event_repository import MarketEventRepository
from tfx_quant.domain.market_data import RawMarketEvent


class MarketDataRecorderService:
    def __init__(
        self,
        repository: MarketEventRepository,
        parser: MarketEventParser,
        aggregator: RealtimeBarAggregator,
        on_closed: Callable[[ClosedAggregation], None],
    ) -> None:
        self._repository = repository
        self._parser = parser
        self._aggregator = aggregator
        self._on_closed = on_closed

    def record(self, raw: RawMarketEvent) -> bool:
        normalized = self._parser.parse(raw)
        # The durable commit is intentionally before aggregation/publication.
        if not self._repository.append(normalized):
            return False
        for closed in self._aggregator.accept(normalized):
            self._on_closed(closed)
        return True
