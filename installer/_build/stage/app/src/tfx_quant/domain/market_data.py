"""Vendor-neutral values used by the local Yuanta quote recorder."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from tfx_quant.domain.timestamp import Timestamp


class MarketEventQuality(StrEnum):
    VALID_TRADE = "VALID_TRADE"
    PRE_MARKET = "PRE_MARKET"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"


@dataclass(frozen=True, slots=True)
class RawMarketEvent:
    """One actual ``OnGetMktAll`` callback, before any numeric conversion."""

    symbol: str
    sequence: int
    session_id: str
    received_at: Timestamp
    fields: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must not be blank")
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        if not self.session_id.strip():
            raise ValueError("session_id must not be blank")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True, slots=True)
class RecordedMarketEvent:
    """Persisted callback plus its conservative normalization result."""

    raw: RawMarketEvent
    quality: MarketEventQuality
    match_time_raw: str
    matched_at: Timestamp | None
    matched_at_taipei: datetime | None
    match_price: Decimal | None
    match_quantity: int | None
    total_match_quantity: int | None
    rejection_reason: str | None = None

    @property
    def is_trade(self) -> bool:
        return self.quality is MarketEventQuality.VALID_TRADE


@dataclass(frozen=True, slots=True)
class MarketDataGap:
    symbol: str
    start: Timestamp
    end: Timestamp | None
    reason: str
