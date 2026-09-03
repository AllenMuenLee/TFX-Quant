from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
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


def _boundary_resolver(
    session_start: datetime,
) -> Callable[[Timestamp], tuple[Timestamp, Timestamp, date, MarketSession] | None]:
    """The 60-minute grid of one session, as `TradingCalendar` would produce it."""

    def resolve(at: Timestamp) -> tuple[Timestamp, Timestamp, date, MarketSession] | None:
        elapsed = at.value - session_start
        if elapsed < timedelta(0) or elapsed >= timedelta(hours=5):
            return None
        start = session_start + (elapsed // timedelta(hours=1)) * timedelta(hours=1)
        return (
            Timestamp(start),
            Timestamp(start + timedelta(hours=1)),
            session_start.date(),
            MarketSession.DAY,
        )

    return resolve


def _open_labels(recording_started_at: datetime | None, *event_times: datetime) -> list[str]:
    session_start = datetime(2026, 8, 24, 8, 45, tzinfo=TAIPEI_TZ)
    parser = MarketEventParser(lambda raw, _received: datetime.fromisoformat(raw))
    aggregator = RealtimeBarAggregator(
        Instrument.TXF,
        ContractMonth(2026, 8),
        _boundary_resolver(session_start),
        None if recording_started_at is None else Timestamp(recording_started_at),
    )
    closed = []
    for sequence, at in enumerate(event_times, start=1):
        closed += aggregator.accept(parser.parse(_raw(sequence, at, total=str(sequence * 10))))
    closed += aggregator.advance(Timestamp(session_start + timedelta(hours=5)))
    return [aggregation.bar.start.value.strftime("%H:%M") for aggregation in closed]


def test_boundary_already_under_way_at_recording_start_is_dropped_whole() -> None:
    # 09:45-10:45 was already running when recording began at 10:12, so it can only be
    # seen as a fragment: no persisted bar, and nothing to draw as a forming bar either.
    assert _open_labels(
        datetime(2026, 8, 24, 10, 12, tzinfo=TAIPEI_TZ),
        datetime(2026, 8, 24, 10, 20, tzinfo=TAIPEI_TZ),
        datetime(2026, 8, 24, 10, 40, tzinfo=TAIPEI_TZ),
        datetime(2026, 8, 24, 11, 0, tzinfo=TAIPEI_TZ),
    ) == ["10:45"]


def test_recording_started_exactly_on_an_open_label_keeps_that_boundary() -> None:
    # Nothing of 10:45-11:45 was missed, so it is a real bar.
    assert _open_labels(
        datetime(2026, 8, 24, 10, 45, 0, tzinfo=TAIPEI_TZ),
        datetime(2026, 8, 24, 10, 45, 0, tzinfo=TAIPEI_TZ),
        datetime(2026, 8, 24, 11, 30, tzinfo=TAIPEI_TZ),
    ) == ["10:45"]


def test_forming_bar_of_a_dropped_boundary_is_never_exposed() -> None:
    session_start = datetime(2026, 8, 24, 8, 45, tzinfo=TAIPEI_TZ)
    parser = MarketEventParser(lambda raw, _received: datetime.fromisoformat(raw))
    aggregator = RealtimeBarAggregator(
        Instrument.TXF,
        ContractMonth(2026, 8),
        _boundary_resolver(session_start),
        Timestamp(datetime(2026, 8, 24, 10, 12, tzinfo=TAIPEI_TZ)),
    )

    aggregator.accept(parser.parse(_raw(1, datetime(2026, 8, 24, 10, 20, tzinfo=TAIPEI_TZ))))

    assert aggregator.forming_bar is None


def test_without_a_recording_start_every_resolved_boundary_still_aggregates() -> None:
    assert _open_labels(
        None,
        datetime(2026, 8, 24, 10, 20, tzinfo=TAIPEI_TZ),
        datetime(2026, 8, 24, 11, 0, tzinfo=TAIPEI_TZ),
    ) == ["09:45", "10:45"]
