"""infrastructure.market_data exceptions — not `DomainError` subclasses (infrastructure-
layer I/O failures, not domain-rule violations)."""

from __future__ import annotations


class TradingCalendarFileError(Exception):
    """The controlled 交易日曆 JSON file is missing, malformed, or internally
    inconsistent — raised at load time so a bad file fails loudly at startup."""
