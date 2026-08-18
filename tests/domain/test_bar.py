from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)


def _ts(hour: int, minute: int) -> Timestamp:
    return Timestamp(datetime(2026, 9, 16, hour, minute, tzinfo=TAIPEI_TZ))


def _bar(open_: str, close: str, high: str | None = None, low: str | None = None) -> Bar:
    high_amount = Decimal(high) if high is not None else max(Decimal(open_), Decimal(close))
    low_amount = Decimal(low) if low is not None else min(Decimal(open_), Decimal(close))
    return Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        open=Price(Decimal(open_)),
        high=Price(high_amount),
        low=Price(low_amount),
        close=Price(Decimal(close)),
        volume=10,
        start=_ts(8, 45),
        end=_ts(9, 45),
    )


def test_candle_color_red_when_close_above_open() -> None:
    assert _bar("100", "105").candle_color is CandleColor.RED


def test_candle_color_black_when_close_below_open() -> None:
    assert _bar("105", "100").candle_color is CandleColor.BLACK


def test_candle_color_doji_when_close_equals_open() -> None:
    assert _bar("100", "100").candle_color is CandleColor.DOJI


def test_bar_start_is_label_open_time_end_is_close_time() -> None:
    bar = _bar("100", "105")
    assert bar.start.value.hour == 8 and bar.start.value.minute == 45
    assert bar.end.value.hour == 9 and bar.end.value.minute == 45
