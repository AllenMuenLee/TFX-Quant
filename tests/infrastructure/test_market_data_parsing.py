from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest

from tfx_quant.infrastructure.yuanta.errors import MarketDataParseError
from tfx_quant.infrastructure.yuanta.market_data_parsing import parse_market_data_push


def _push(**overrides: str) -> dict[str, str]:
    defaults = {
        "symbol": "TXFU6",
        "match_time": "093015",
        "match_pri": "17500",
        "match_qty": "1",
        "tol_match_qty": "42",
    }
    defaults.update(overrides)
    return defaults


def test_parses_valid_hhmmss_push() -> None:
    result = parse_market_data_push(**_push())
    assert result is not None
    assert result.vendor_symbol == "TXFU6"
    assert result.price == Decimal("17500")
    assert result.size == 1
    assert result.cumulative_volume == 42
    assert result.exchange_time == time(9, 30, 15)


def test_parses_valid_hhmmssmmm_push_with_milliseconds() -> None:
    result = parse_market_data_push(**_push(match_time="093015123"))
    assert result is not None
    assert result.exchange_time == time(9, 30, 15, 123_000)


def test_pre_market_sentinel_returns_none() -> None:
    assert parse_market_data_push(**_push(tol_match_qty="-1")) is None


def test_rejects_blank_symbol() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(symbol="   "))


def test_rejects_malformed_match_time_length() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(match_time="9301"))


def test_rejects_out_of_range_match_time() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(match_time="996099"))


def test_rejects_non_numeric_price() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(match_pri="abc"))


def test_rejects_non_positive_price() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(match_pri="0"))


def test_rejects_non_numeric_size() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(match_qty="abc"))


def test_rejects_non_numeric_cumulative_volume() -> None:
    with pytest.raises(MarketDataParseError):
        parse_market_data_push(**_push(tol_match_qty="abc"))
