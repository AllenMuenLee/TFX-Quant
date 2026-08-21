"""MockYahooHistoryQuery — the `use_mock: true` stand-in for `YahooHistoryQueryPort`.

Always returns an empty result: mock mode has no real network access to Yahoo Finance,
so every requested range simply stays a gap rather than inventing fake historical bars —
mirrors `infrastructure.yuanta.mock_historical_price_query.MockHistoricalPriceQuery`'s
precedent for the (now superseded) vendor `GetKLine` path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from tfx_quant.application.ports.yahoo_history_query import YahooBar


class MockYahooHistoryQuery:
    """Implements `application.ports.yahoo_history_query.YahooHistoryQueryPort`."""

    def query_1h_bars(
        self, *, yahoo_ticker: str, start_date: date, end_date: date
    ) -> Sequence[YahooBar]:
        return ()
