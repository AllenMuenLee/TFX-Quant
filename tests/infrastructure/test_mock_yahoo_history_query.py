from __future__ import annotations

from datetime import date, time

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.infrastructure.market_data.mock_yahoo_history_query import (
    MockYahooHistoryQuery,
    MockYahooTickerMappingRepository,
)


def test_mock_mapping_covers_any_controlled_contract_identity() -> None:
    mapping = MockYahooTickerMappingRepository()

    assert mapping.get(Instrument.MXF, ContractMonth(year=2026, month=9)) == ("MOCK-MXF-202609")


def test_mock_history_returns_deterministic_canonical_hourly_bars() -> None:
    query = MockYahooHistoryQuery()

    first = query.query_1h_bars(
        yahoo_ticker="MOCK-MXF-202609",
        start_date=date(2026, 8, 21),
        end_date=date(2026, 8, 21),
    )
    second = query.query_1h_bars(
        yahoo_ticker="MOCK-MXF-202609",
        start_date=date(2026, 8, 21),
        end_date=date(2026, 8, 21),
    )

    assert first == second
    assert len(first) == 19
    assert first[0].at.timetz().replace(tzinfo=None) == time(8, 45)
    assert first[-1].at.timetz().replace(tzinfo=None) == time(4, 0)


def test_mock_history_skips_weekends() -> None:
    rows = MockYahooHistoryQuery().query_1h_bars(
        yahoo_ticker="MOCK-TXF-202609",
        start_date=date(2026, 8, 22),
        end_date=date(2026, 8, 23),
    )

    assert rows == []
