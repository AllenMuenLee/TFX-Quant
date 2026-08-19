from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

import pytest

from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.infrastructure.yuanta.errors import MarketDataParseError
from tfx_quant.infrastructure.yuanta.market_data_parsing import (
    parse_kline_bar,
    parse_stock_tick_push,
)


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


def _kline(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "year": 2026,
        "month": 8,
        "day": 3,
        "hour": 9,
        "minute": 45,
        "second": 0,
        "open_price": "17500",
        "high_price": "17550",
        "low_price": "17480",
        "close_price": "17520",
        "deal_vol": 120,
    }
    defaults.update(overrides)
    return defaults


def test_parses_valid_kline_bar() -> None:
    result = parse_kline_bar(**_kline())
    assert result.at == datetime(2026, 8, 3, 9, 45, 0, tzinfo=TAIPEI_TZ)
    assert result.open == Decimal("17500")
    assert result.high == Decimal("17550")
    assert result.low == Decimal("17480")
    assert result.close == Decimal("17520")
    assert result.volume == 120


def test_kline_rejects_out_of_range_date() -> None:
    with pytest.raises(MarketDataParseError):
        parse_kline_bar(**_kline(month=13))


def test_kline_rejects_high_below_low() -> None:
    with pytest.raises(MarketDataParseError):
        parse_kline_bar(**_kline(high_price="17000", low_price="17500"))


def test_kline_rejects_negative_volume() -> None:
    with pytest.raises(MarketDataParseError):
        parse_kline_bar(**_kline(deal_vol=-1))


def test_kline_rejects_non_numeric_price() -> None:
    with pytest.raises(MarketDataParseError):
        parse_kline_bar(**_kline(open_price="abc"))
