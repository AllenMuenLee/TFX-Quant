from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.bar_aggregator import CandleStreakCounter
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_WEDNESDAY = date(2026, 9, 16)


def _ts(hour: int, minute: int) -> Timestamp:
    return Timestamp(
        datetime(_WEDNESDAY.year, _WEDNESDAY.month, _WEDNESDAY.day, hour, minute, tzinfo=TAIPEI_TZ)
    )


def _bar(*, open_: str, high: str, low: str, close: str, start_hour: int) -> Bar:
    return Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        open=Price(Decimal(open_)),
        high=Price(Decimal(high)),
        low=Price(Decimal(low)),
        close=Price(Decimal(close)),
        volume=1,
        start=_ts(start_hour, 45),
        end=_ts(start_hour + 1, 45),
    )


def test_candle_streak_counter_tracks_consecutive_same_color() -> None:
    counter = CandleStreakCounter()
    counter.on_bar_closed(_bar(open_="100", high="105", low="100", close="105", start_hour=8))
    assert counter.color is CandleColor.RED
    assert counter.length == 1

    counter.on_bar_closed(_bar(open_="110", high="115", low="110", close="115", start_hour=9))
    assert counter.color is CandleColor.RED
    assert counter.length == 2


def test_candle_streak_counter_black_streak() -> None:
    counter = CandleStreakCounter()
    counter.on_bar_closed(_bar(open_="105", high="105", low="100", close="100", start_hour=8))
    counter.on_bar_closed(_bar(open_="100", high="100", low="95", close="95", start_hour=9))
    assert counter.color is CandleColor.BLACK
    assert counter.length == 2


def test_candle_streak_counter_color_change_resets_length_to_one() -> None:
    counter = CandleStreakCounter()
    counter.on_bar_closed(_bar(open_="100", high="105", low="100", close="105", start_hour=8))
    counter.on_bar_closed(_bar(open_="105", high="105", low="95", close="95", start_hour=9))
    assert counter.color is CandleColor.BLACK
    assert counter.length == 1


def test_candle_streak_counter_doji_resets_streak() -> None:
    counter = CandleStreakCounter()
    counter.on_bar_closed(_bar(open_="100", high="105", low="100", close="105", start_hour=8))
    assert counter.length == 1

    counter.on_bar_closed(_bar(open_="110", high="110", low="100", close="110", start_hour=9))
    assert counter.color is None
    assert counter.length == 0
