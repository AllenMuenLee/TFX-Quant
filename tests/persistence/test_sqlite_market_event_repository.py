from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from tfx_quant.domain.market_data import MarketEventQuality, RawMarketEvent, RecordedMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.persistence.sqlite_market_event_repository import SqliteMarketEventRepository

_SYMBOL = "MXFJ6"


def _at(minute: int, second: int = 0, micro: int = 0) -> Timestamp:
    return Timestamp(datetime(2026, 10, 12, 21, minute, second, micro, tzinfo=TAIPEI_TZ))


def _repository(
    trades: list[tuple[Timestamp, str, MarketEventQuality]],
) -> SqliteMarketEventRepository:
    repository = SqliteMarketEventRepository(sqlite3.connect(":memory:"))
    for sequence, (at, price, quality) in enumerate(trades, start=1):
        raw = RawMarketEvent(_SYMBOL, sequence, "session", at, {"MatchPri": price})
        repository.append(
            RecordedMarketEvent(
                raw, quality, "raw", at, at.value, Decimal(price), 1, sequence, None
            )
        )
    return repository


def test_first_trade_beyond_finds_the_earliest_valid_crossing_in_the_window() -> None:
    repository = _repository(
        [
            (_at(30), "49100", MarketEventQuality.VALID_TRADE),
            (_at(34, 44), "49000", MarketEventQuality.PRE_MARKET),
            (_at(34, 44, 500_000), "49073", MarketEventQuality.VALID_TRADE),
            (_at(34, 45), "49060", MarketEventQuality.VALID_TRADE),
            (_at(59), "49600", MarketEventQuality.VALID_TRADE),
        ]
    )

    hit = repository.first_trade_beyond(
        _SYMBOL,
        since=_at(0),
        before=_at(59),
        at_or_below=Decimal("49073"),
        at_or_above=Decimal("49500"),
    )
    assert hit is not None
    assert (hit.at, hit.price) == (_at(34, 44, 500_000), Decimal("49073"))

    after = repository.first_trade_beyond(
        _SYMBOL,
        since=hit,
        before=_at(59),
        at_or_below=Decimal("49073"),
        at_or_above=Decimal("49500"),
    )
    assert after is not None and after.price == Decimal("49060")
    # `before` is exclusive: the 21:59 trade belongs to the next window.
    assert (
        repository.first_trade_beyond(
            _SYMBOL,
            since=after,
            before=_at(59),
            at_or_below=Decimal("49073"),
            at_or_above=Decimal("49500"),
        )
        is None
    )


def test_last_trade_at_or_before_includes_the_instant_itself() -> None:
    repository = _repository(
        [
            (_at(10), "49100", MarketEventQuality.VALID_TRADE),
            (_at(20), "49110", MarketEventQuality.VALID_TRADE),
            (_at(30), "49120", MarketEventQuality.VALID_TRADE),
        ]
    )

    last = repository.last_trade_at_or_before(_SYMBOL, since=_at(0), at=_at(20))

    assert last is not None and (last.at, last.price) == (_at(20), Decimal("49110"))
    assert repository.last_trade_at_or_before(_SYMBOL, since=last, at=_at(25)) is None
