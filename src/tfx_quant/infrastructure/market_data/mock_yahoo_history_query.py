"""Deterministic market-data simulation used when ``use_mock`` is enabled.

Mock mode must be usable as an end-to-end desktop demonstration.  Returning an empty
sequence made the application's automatic startup refill impossible by construction.
The rows below are explicitly synthetic, deterministic fixtures; they never run in the
real-yfinance branch and are never presented as broker or exchange data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from tfx_quant.application.ports.yahoo_history_query import YahooBar
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.timestamp import TAIPEI_TZ

_DAY_BAR_OPENS = (time(8, 45), time(9, 45), time(10, 45), time(11, 45), time(12, 45))
_NIGHT_BAR_OPENS = tuple(time(hour, 0) for hour in range(15, 24)) + tuple(
    time(hour, 0) for hour in range(0, 5)
)


class MockYahooTickerMappingRepository:
    """Mock-only mapping for every controlled contract in the instrument master."""

    def get(self, instrument: Instrument, contract: ContractMonth) -> str:
        return f"MOCK-{instrument.value}-{contract.code}"


class MockYahooHistoryQuery:
    """Implements `application.ports.yahoo_history_query.YahooHistoryQueryPort`."""

    def query_1h_bars(
        self, *, yahoo_ticker: str, start_date: date, end_date: date
    ) -> Sequence[YahooBar]:
        del yahoo_ticker  # identity is carried by the mock mapping and caller
        rows: list[YahooBar] = []
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:
                rows.extend(self._bars_for_date(current))
            current += timedelta(days=1)
        return rows

    @staticmethod
    def _bars_for_date(value: date) -> list[YahooBar]:
        rows: list[YahooBar] = []
        for index, open_time in enumerate((*_DAY_BAR_OPENS, *_NIGHT_BAR_OPENS)):
            # Stable but visibly changing OHLCV values make the desktop useful without
            # pretending that simulation output is real market history.
            base = Decimal(20_000 + (value.toordinal() % 1_000) + index * 3)
            rows.append(
                YahooBar(
                    at=datetime.combine(value, open_time, tzinfo=TAIPEI_TZ),
                    open=base,
                    high=base + Decimal(8),
                    low=base - Decimal(5),
                    close=base + Decimal(2),
                    volume=100 + index,
                )
            )
        return rows
