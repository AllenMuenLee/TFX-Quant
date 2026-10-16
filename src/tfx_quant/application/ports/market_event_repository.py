from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from tfx_quant.domain.market_data import MarketDataGap, RecordedMarketEvent
from tfx_quant.domain.timestamp import Timestamp


@dataclass(frozen=True, slots=True)
class TradePrint:
    """One persisted valid match. `position` orders prints that share a match time."""

    at: Timestamp
    price: Decimal
    position: tuple[str, str, int]


class MarketEventRepository(Protocol):
    def append(self, event: RecordedMarketEvent) -> bool:
        """Commit an event. Return False for an identical session/sequence replay."""
        ...

    def list_events(self, symbol: str) -> Sequence[RecordedMarketEvent]: ...

    def record_gap(self, gap: MarketDataGap) -> None: ...

    def list_gaps(self, symbol: str) -> Sequence[MarketDataGap]: ...

    def first_trade_beyond(
        self,
        symbol: str,
        *,
        since: TradePrint | Timestamp,
        before: Timestamp,
        at_or_below: Decimal,
        at_or_above: Decimal,
    ) -> TradePrint | None:
        """The earliest valid match in `[since, before)` priced `<= at_or_below` or
        `>= at_or_above`. A `TradePrint` as `since` means strictly after that print."""
        ...

    def last_trade_at_or_before(
        self, symbol: str, *, since: TradePrint | Timestamp, at: Timestamp
    ) -> TradePrint | None:
        """The latest valid match in `[since, at]` (strictly after a `TradePrint`)."""
        ...
