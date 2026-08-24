"""Fail-closed compatibility seam for the removed external-history workflow."""

from __future__ import annotations

from datetime import date

from tfx_quant.application.ports.yahoo_history_query import YahooBar, YahooHistoryQueryError


class ExternalHistoryDisabledQuery:
    def query_1h_bars(
        self, *, yahoo_ticker: str, start_date: date, end_date: date
    ) -> list[YahooBar]:
        del yahoo_ticker, start_date, end_date
        raise YahooHistoryQueryError(
            "External history is disabled; only locally recorded Yuanta events are allowed"
        )
