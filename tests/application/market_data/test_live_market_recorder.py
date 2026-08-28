from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from tfx_quant.application.market_data.event_parser import MarketEventParser
from tfx_quant.application.market_data.realtime_bar_aggregator import RealtimeBarAggregator
from tfx_quant.application.market_data.recorder_service import MarketDataRecorderService
from tfx_quant.domain.bar_record import MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.market_data import MarketEventQuality, RawMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.persistence.sqlite_market_event_repository import SqliteMarketEventRepository


def _raw(sequence: int, at: datetime, *, total: str = "10") -> RawMarketEvent:
    return RawMarketEvent(
        "TXFA6",
        sequence,
        "session-1",
        Timestamp(at),
        {"MatchTime": at.isoformat(), "MatchPri": "18000.5", "MatchQty": "2", "TolMatchQty": total},
    )


def test_pre_market_is_persisted_but_not_aggregated() -> None:
    at = datetime(2026, 8, 24, 8, 30, tzinfo=TAIPEI_TZ)
    repository = SqliteMarketEventRepository(sqlite3.connect(":memory:"))
    parser = MarketEventParser(lambda raw, _received: datetime.fromisoformat(raw))
    aggregator = RealtimeBarAggregator(Instrument.TXF, ContractMonth(2026, 8), lambda _at: None)
    closed: list[object] = []
    service = MarketDataRecorderService(repository, parser, aggregator, closed.append)

    assert service.record(_raw(1, at, total="-1"))
    assert repository.list_events("TXFA6")[0].quality is MarketEventQuality.PRE_MARKET
    assert closed == []


def test_commit_precedes_aggregation_and_duplicate_sequence_is_idempotent() -> None:
    at = datetime(2026, 8, 24, 8, 45, tzinfo=TAIPEI_TZ)
    end = at + timedelta(hours=1)
    repository = SqliteMarketEventRepository(sqlite3.connect(":memory:"))
    parser = MarketEventParser(lambda raw, _received: datetime.fromisoformat(raw))
    aggregator = RealtimeBarAggregator(
        Instrument.TXF,
        ContractMonth(2026, 8),
        lambda _at: (Timestamp(at), Timestamp(end), at.date(), MarketSession.DAY),
    )
    closed: list[object] = []
    service = MarketDataRecorderService(repository, parser, aggregator, closed.append)

    assert service.record(_raw(1, at))
    assert not service.record(_raw(1, at))
    result = aggregator.advance(Timestamp(end))
    assert result[0].bar.volume == 2
    assert result[0].bar.open.amount == Decimal("18000.5")


def test_unknown_match_time_format_fails_closed_but_keeps_raw_event() -> None:
    at = datetime(2026, 8, 24, 8, 45, tzinfo=TAIPEI_TZ)
    parsed = MarketEventParser().parse(_raw(1, at))
    assert parsed.quality is MarketEventQuality.REJECTED
    assert parsed.match_time_raw == at.isoformat()
    assert parsed.rejection_reason == "MatchTime format is not configured"
