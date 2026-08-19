"""A mock HistoricalPriceQueryPort — no COM/network calls, used by default
(`use_mock: true`) so the whole codebase builds and tests without the vendor API
installed.

Always returns an empty result — mock mode has no vendor session to actually query, so
every requested range simply stays an unresolved gap, matching this codebase's "never
fabricate" policy rather than inventing fake historical bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from tfx_quant.application.ports.historical_price_query import VendorKLineBar
from tfx_quant.domain.account import TradingAccount


class MockHistoricalPriceQuery:
    """Implements `application.ports.historical_price_query.HistoricalPriceQueryPort`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []

    def query_60m_kline(
        self,
        *,
        account: TradingAccount,
        vendor_symbol: str,
        start_date: date,
        end_date: date,
    ) -> Sequence[VendorKLineBar]:
        del account  # unused — mock mode never authenticates against a real vendor
        self.calls.append((vendor_symbol, start_date, end_date))
        return ()
