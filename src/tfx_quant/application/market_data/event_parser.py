"""Strict normalization for the fields documented for ``OnGetMktAll``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation

from tfx_quant.domain.market_data import (
    MarketEventQuality,
    RawMarketEvent,
    RecordedMarketEvent,
)
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

MatchTimeParser = Callable[[str, datetime], datetime]


class MarketEventParser:
    """Parses only documented fields; time syntax is supplied by configuration.

    The vendor PDF names ``MatchTime`` but does not define its syntax.  Consequently
    there is deliberately no built-in format guess.  Without a configured parser the
    callback remains recorded but is rejected for aggregation.
    """

    def __init__(self, match_time_parser: MatchTimeParser | None = None) -> None:
        self._match_time_parser = match_time_parser

    def parse(self, raw: RawMarketEvent) -> RecordedMarketEvent:
        fields = raw.fields
        match_time_raw = fields.get("MatchTime", "")
        total_raw = fields.get("TolMatchQty", "")
        if total_raw.strip() == "-1":
            return RecordedMarketEvent(
                raw=raw,
                quality=MarketEventQuality.PRE_MARKET,
                match_time_raw=match_time_raw,
                matched_at=None,
                matched_at_taipei=None,
                match_price=None,
                match_quantity=None,
                total_match_quantity=-1,
            )
        try:
            price = _decimal(fields.get("MatchPri", ""), "MatchPri")
            quantity = _non_negative_int(fields.get("MatchQty", ""), "MatchQty")
            total = _non_negative_int(total_raw, "TolMatchQty")
        except ValueError as exc:
            return _rejected(raw, match_time_raw, str(exc))
        if self._match_time_parser is None:
            return _rejected(raw, match_time_raw, "MatchTime format is not configured")
        try:
            local = self._match_time_parser(match_time_raw, raw.received_at.value)
            if local.tzinfo is None or local.utcoffset() is None:
                raise ValueError("configured MatchTime parser returned a naive datetime")
            local = local.astimezone(TAIPEI_TZ)
            matched_at = Timestamp(local)
        except (ValueError, OverflowError) as exc:
            return _rejected(raw, match_time_raw, f"invalid MatchTime: {exc}")
        return RecordedMarketEvent(
            raw=raw,
            quality=MarketEventQuality.VALID_TRADE,
            match_time_raw=match_time_raw,
            matched_at=matched_at,
            matched_at_taipei=local,
            match_price=price,
            match_quantity=quantity,
            total_match_quantity=total,
        )


def _decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{label} is not a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be a finite non-negative decimal")
    return parsed


def _non_negative_int(value: str, label: str) -> int:
    try:
        parsed = int(value.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} is not an integer") from exc
    if parsed < 0:
        raise ValueError(f"{label} must be non-negative")
    return parsed


def _rejected(raw: RawMarketEvent, match_time_raw: str, reason: str) -> RecordedMarketEvent:
    return RecordedMarketEvent(
        raw=raw,
        quality=MarketEventQuality.REJECTED,
        match_time_raw=match_time_raw,
        matched_at=None,
        matched_at_taipei=None,
        match_price=None,
        match_quantity=None,
        total_match_quantity=None,
        rejection_reason=reason,
    )
