from __future__ import annotations

from datetime import time
from decimal import Decimal

import pytest

from tfx_quant.infrastructure.yuanta.errors import MarketDataParseError
from tfx_quant.infrastructure.yuanta.market_data_parsing import parse_stock_tick_push


def _push(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "stk_code": "TXFU6",
        "serial_no": 42,
        "deal_price": "17500",
        "deal_vol": 1,
        "hour": 9,
        "minute": 30,
        "second": 15,
        "millisecond": 0,
    }
    defaults.update(overrides)
    return defaults


def test_parses_valid_push() -> None:
    result = parse_stock_tick_push(**_push())
    assert result is not None
    assert result.vendor_symbol == "TXFU6"
    assert result.price == Decimal("17500")
    assert result.size == 1
    assert result.serial_no == 42
    assert result.exchange_time == time(9, 30, 15)


def test_parses_push_with_milliseconds() -> None:
    result = parse_stock_tick_push(**_push(millisecond=123))
    assert result is not None
    assert result.exchange_time == time(9, 30, 15, 123_000)


def test_settlement_sentinel_returns_none() -> None:
    assert parse_stock_tick_push(**_push(serial_no=-1)) is None


def test_rejects_blank_stk_code() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(stk_code="   "))


def test_rejects_serial_no_below_one_and_not_settlement_sentinel() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(serial_no=0))


def test_rejects_out_of_range_time() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(hour=99, minute=99, second=99))


def test_rejects_non_numeric_price() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(deal_price="abc"))


def test_rejects_non_positive_price() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(deal_price="0"))


def test_rejects_negative_size() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(deal_vol=-1))


def test_rejects_non_numeric_serial_no() -> None:
    with pytest.raises(MarketDataParseError):
        parse_stock_tick_push(**_push(serial_no="abc"))
