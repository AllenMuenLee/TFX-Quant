from __future__ import annotations

import time as _real_time
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import (
    BarClosed,
    BarPersistenceHealthChanged,
    BarRetentionCleanupCompleted,
    BrokerSessionReady,
    Event,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
    MarketDataTickReceived,
)
from tfx_quant.application.market_data.bar_service import MarketDataBarService
from tfx_quant.application.ports.bar_record_repository import (
    BarRecordNotFoundError,
    BarUpsertOutcome,
    BarUpsertRepositoryError,
    RetentionCleanupSummary,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord, MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_WEDNESDAY = date(2026, 9, 16)
_VENDOR_SYMBOL = "TXFU6"
_ACCOUNT = TradingAccount(branch_id="F00", account_no="9808900")


class FakeEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        self.published: list[Event] = []

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        self.published.append(event)
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in list(handlers):
                    handler(event)


class FakeClock:
    def __init__(self, now: Timestamp) -> None:
        self._now = now

    def now(self) -> Timestamp:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = Timestamp(self._now.value + timedelta(seconds=seconds))


class FakeTradingCalendarRepository:
    def __init__(
        self, holidays: frozenset[date] = frozenset(), early_closes: dict[date, time] | None = None
    ) -> None:
        self._holidays = holidays
        self._early_closes = early_closes or {}

    def get_holidays(self) -> frozenset[date]:
        return self._holidays

    def get_early_closes(self) -> dict[date, time]:
        return self._early_closes


class FakeInstrumentMasterRepository:
    def __init__(self, entries: dict[tuple[Instrument, ContractMonth], InstrumentMasterEntry]):
        self._entries = entries

    def get(self, instrument: Instrument, contract: ContractMonth) -> InstrumentMasterEntry | None:
        return self._entries.get((instrument, contract))

    def list_for(self, instrument: Instrument) -> list[InstrumentMasterEntry]:
        return [e for (i, _c), e in self._entries.items() if i == instrument]


def _entry() -> InstrumentMasterEntry:
    return InstrumentMasterEntry(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        vendor_symbol=_VENDOR_SYMBOL,
        broker_product_code="TXF",
        tick_size=Decimal("1"),
        multiplier=Decimal("200"),
        day_session_start=time(8, 45),
        day_session_end=time(13, 45),
        night_session_start=time(15, 0),
        night_session_end=time(5, 0),
        expiry_date=date(2026, 9, 16),
        tradable=True,
    )


def _ts(hour: int, minute: int, second: int = 0, d: date = _WEDNESDAY) -> Timestamp:
    return Timestamp(datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=TAIPEI_TZ))


def _bar_ohlcv_matches(a: Bar, b: Bar) -> bool:
    return (
        a.open.amount == b.open.amount
        and a.high.amount == b.high.amount
        and a.low.amount == b.low.amount
        and a.close.amount == b.close.amount
        and a.volume == b.volume
        and a.end.value == b.end.value
    )


class FakeBarRecordRepository:
    """In-memory stand-in for `application.ports.bar_record_repository.
    BarRecordRepository` — mirrors `SqliteBarRecordRepository`'s dedup/conflict/
    retention semantics without touching a real database, plus test-only knobs
    (`fail_writes_remaining`) for exercising the bounded-retry/degraded path."""

    def __init__(self) -> None:
        self._rows: dict[tuple[Instrument, ContractMonth, BarPeriod, Timestamp], BarRecord] = {}
        self.fail_writes_remaining = 0
        self.delete_before_calls: list[date] = []

    def all_records(self) -> list[BarRecord]:
        return sorted(self._rows.values(), key=lambda r: r.bar.start.value)

    def upsert_closed_bar(self, record: BarRecord) -> BarUpsertOutcome:
        if self.fail_writes_remaining > 0:
            self.fail_writes_remaining -= 1
            raise BarUpsertRepositoryError("simulated write failure")
        existing = self._rows.get(record.identity)
        if existing is None:
            self._rows[record.identity] = record
            return BarUpsertOutcome.INSERTED
        if _bar_ohlcv_matches(existing.bar, record.bar):
            return BarUpsertOutcome.DUPLICATE_IGNORED
        return BarUpsertOutcome.CONFLICT_REJECTED

    def apply_correction(self, record: BarRecord, *, reason: str) -> None:
        existing = self._rows.get(record.identity)
        if existing is None:
            raise BarRecordNotFoundError(f"no existing row for {record.identity!r}")
        del reason  # audit trail isn't modeled by this fake — only SqliteBarRecordRepository is
        self._rows[record.identity] = BarRecord(
            bar=record.bar,
            period=record.period,
            trading_day=record.trading_day,
            session=record.session,
            source=record.source,
            is_gap_recovery=record.is_gap_recovery,
            created_at=existing.created_at,
            updated_at=record.updated_at,
            revision=existing.revision + 1,
        )

    def list_recent(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        since_trading_day: date,
    ) -> list[BarRecord]:
        return [
            r
            for r in self.all_records()
            if r.bar.instrument == instrument
            and r.bar.contract == contract
            and r.period == period
            and r.trading_day >= since_trading_day
        ]

    def query_range(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        start_date: date,
        end_date: date,
    ) -> list[BarRecord]:
        return [
            r
            for r in self.all_records()
            if r.bar.instrument == instrument
            and r.bar.contract == contract
            and r.period == period
            and start_date <= r.trading_day <= end_date
        ]

    def earliest_recorded_at(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod
    ) -> Timestamp | None:
        rows = self.list_recent(instrument, contract, period, since_trading_day=date.min)
        return rows[0].bar.start if rows else None

    def latest_recorded_at(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod
    ) -> Timestamp | None:
        rows = self.list_recent(instrument, contract, period, since_trading_day=date.min)
        return rows[-1].bar.start if rows else None

    def delete_before(
        self, cutoff_trading_day: date, *, ran_at: Timestamp
    ) -> RetentionCleanupSummary:
        self.delete_before_calls.append(cutoff_trading_day)
        before = len(self._rows)
        self._rows = {k: v for k, v in self._rows.items() if v.trading_day >= cutoff_trading_day}
        return RetentionCleanupSummary(
            cutoff_trading_day=cutoff_trading_day,
            deleted_count=before - len(self._rows),
            ran_at=ran_at,
        )


def _insert_record(
    repo: FakeBarRecordRepository,
    start: Timestamp,
    end: Timestamp,
    *,
    open_: str,
    close: str,
) -> Bar:
    low, high = sorted((Decimal(open_), Decimal(close)))
    bar = Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        open=Price(Decimal(open_)),
        high=Price(high),
        low=Price(low),
        close=Price(Decimal(close)),
        volume=5,
        start=start,
        end=end,
    )
    record = BarRecord(
        bar=bar,
        period=BarPeriod.SIXTY_MINUTE,
        trading_day=start.value.date(),
        session=MarketSession.DAY,
        source=BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME,
        is_gap_recovery=False,
        created_at=start,
        updated_at=start,
    )
    repo.upsert_closed_bar(record)
    return bar


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = _real_time.monotonic() + timeout
    while _real_time.monotonic() < deadline:
        if predicate():
            return
        _real_time.sleep(0.01)
    assert predicate(), "condition not met within timeout"


def _build(
    *,
    now: Timestamp | None = None,
    stale_after_seconds: float = 10.0,
    bar_record_repository: FakeBarRecordRepository | None = None,
    write_queue_maxsize: int = 500,
    max_write_retries: int = 5,
) -> tuple[MarketDataBarService, FakeEventBus, FakeClock]:
    bus = FakeEventBus()
    clock = FakeClock(now or _ts(8, 40))
    service = MarketDataBarService(
        event_bus=bus,
        clock=clock,
        trading_calendar_repository=FakeTradingCalendarRepository(),
        instrument_master=FakeInstrumentMasterRepository({(_INSTRUMENT, _CONTRACT): _entry()}),
        bar_record_repository=bar_record_repository or FakeBarRecordRepository(),
        stale_after_seconds=stale_after_seconds,
        write_queue_maxsize=write_queue_maxsize,
        max_write_retries=max_write_retries,
    )
    return service, bus, clock


def _push(
    service: MarketDataBarService,
    bus: FakeEventBus,
    at: Timestamp,
    exchange_time: time,
    price: str,
    serial_no: int,
    size: int = 1,
    vendor_symbol: str = _VENDOR_SYMBOL,
) -> None:
    bus.publish(
        MarketDataTickReceived(
            at=at,
            vendor_symbol=vendor_symbol,
            price=Decimal(price),
            size=size,
            serial_no=serial_no,
            exchange_time=exchange_time,
        )
    )


def test_clear_with_no_master_entry_leaves_inactive() -> None:
    bus = FakeEventBus()
    clock = FakeClock(_ts(8, 40))
    service = MarketDataBarService(
        event_bus=bus,
        clock=clock,
        trading_calendar_repository=FakeTradingCalendarRepository(),
        instrument_master=FakeInstrumentMasterRepository({}),
        bar_record_repository=FakeBarRecordRepository(),
    )
    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is True


def test_clear_activates_contract_with_empty_forming_bar() -> None:
    service, _bus, _clock = _build()
    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None
    assert service.recent_closed_bars(_INSTRUMENT, _CONTRACT) == ()
    assert service.has_gap(_INSTRUMENT, _CONTRACT) is False


def test_tick_for_inactive_contract_is_ignored() -> None:
    service, bus, clock = _build()
    _push(service, bus, clock.now(), time(9, 0), "17500", serial_no=1)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None


def test_tick_with_symbol_mismatch_is_ignored() -> None:
    service, bus, clock = _build()
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(
        service,
        bus,
        clock.now(),
        time(9, 0),
        "17500",
        serial_no=1,
        vendor_symbol="SOMETHING-ELSE",
    )
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None


def test_tick_updates_forming_bar_and_clears_staleness() -> None:
    service, bus, clock = _build(now=_ts(9, 0))
    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is True  # nothing received yet

    _push(service, bus, clock.now(), time(9, 0), "17500", serial_no=1)

    forming = service.forming_bar(_INSTRUMENT, _CONTRACT)
    assert forming is not None
    assert forming.open.amount == Decimal("17500")
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is False
    freshness_events = [e for e in bus.published if isinstance(e, MarketDataFreshnessChanged)]
    assert freshness_events[-1].is_stale is False


def test_bar_closed_published_when_boundary_crossed() -> None:
    service, bus, clock = _build(now=_ts(8, 50))
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)

    clock.advance(60 * 60)  # 09:50
    _push(service, bus, clock.now(), time(9, 50), "17600", serial_no=2)

    closed_events = [e for e in bus.published if isinstance(e, BarClosed)]
    assert len(closed_events) == 1
    assert closed_events[0].bar.start.value.time() == time(8, 45)
    assert list(service.recent_closed_bars(_INSTRUMENT, _CONTRACT)) == [closed_events[0].bar]


def test_on_clock_tick_marks_stale_after_threshold_with_no_new_ticks() -> None:
    service, bus, clock = _build(now=_ts(9, 0), stale_after_seconds=5.0)
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(9, 0), "17500", serial_no=1)
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is False

    clock.advance(10.0)
    service.on_clock_tick()

    assert service.is_stale(_INSTRUMENT, _CONTRACT) is True
    freshness_events = [e for e in bus.published if isinstance(e, MarketDataFreshnessChanged)]
    assert freshness_events[-1].is_stale is True


def test_on_clock_tick_closes_forming_bar_with_no_new_tick() -> None:
    service, bus, clock = _build(now=_ts(8, 50))
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)

    clock.advance(60 * 60)  # past the 09:45 boundary, no new tick arrives
    service.on_clock_tick()

    closed_events = [e for e in bus.published if isinstance(e, BarClosed)]
    assert len(closed_events) == 1
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None


def test_broker_session_ready_flags_gap_until_next_bar_closes_cleanly() -> None:
    service, bus, clock = _build(now=_ts(8, 50))
    service.clear(_INSTRUMENT, _CONTRACT)

    bus.publish(BrokerSessionReady(at=clock.now(), account=_ACCOUNT))
    assert service.has_gap(_INSTRUMENT, _CONTRACT) is True
    gap_events = [e for e in bus.published if isinstance(e, MarketDataGapDetected)]
    assert len(gap_events) == 1

    _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)
    clock.advance(60 * 60)
    _push(service, bus, clock.now(), time(9, 50), "17600", serial_no=2)

    assert service.has_gap(_INSTRUMENT, _CONTRACT) is False
    cleared_events = [e for e in bus.published if isinstance(e, MarketDataGapCleared)]
    assert len(cleared_events) == 1


def test_broker_session_ready_with_no_active_contract_is_a_no_op() -> None:
    service, bus, clock = _build()
    bus.publish(BrokerSessionReady(at=clock.now(), account=_ACCOUNT))
    gap_events = [e for e in bus.published if isinstance(e, MarketDataGapDetected)]
    assert gap_events == []


def test_clear_resets_previous_forming_bar_state() -> None:
    service, bus, clock = _build(now=_ts(8, 50))
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is not None

    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None


# -- Two-month bar-history persistence (implementation prompt extension) -------------


def test_bar_closed_is_persisted_via_background_writer() -> None:
    repo = FakeBarRecordRepository()
    service, bus, clock = _build(now=_ts(8, 50), bar_record_repository=repo)
    service.clear(_INSTRUMENT, _CONTRACT)
    service.start()
    try:
        _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)
        clock.advance(60 * 60)
        _push(service, bus, clock.now(), time(9, 50), "17600", serial_no=2)
        _wait_until(lambda: len(repo.all_records()) == 1)
    finally:
        service.stop()

    records = repo.all_records()
    assert records[0].bar.start.value.time() == time(8, 45)
    assert records[0].period is BarPeriod.SIXTY_MINUTE
    assert records[0].session is MarketSession.DAY
    assert records[0].trading_day == _WEDNESDAY
    assert records[0].source is BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME
    assert records[0].is_gap_recovery is False


def test_bar_closed_after_gap_is_flagged_is_gap_recovery() -> None:
    repo = FakeBarRecordRepository()
    service, bus, clock = _build(now=_ts(8, 50), bar_record_repository=repo)
    service.clear(_INSTRUMENT, _CONTRACT)
    service.start()
    try:
        bus.publish(BrokerSessionReady(at=clock.now(), account=_ACCOUNT))
        _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)
        clock.advance(60 * 60)
        _push(service, bus, clock.now(), time(9, 50), "17600", serial_no=2)
        _wait_until(lambda: len(repo.all_records()) == 1)
    finally:
        service.stop()

    assert repo.all_records()[0].is_gap_recovery is True


def test_write_queue_full_marks_degraded_without_blocking_tick_processing() -> None:
    repo = FakeBarRecordRepository()
    service, bus, clock = _build(now=_ts(8, 50), bar_record_repository=repo, write_queue_maxsize=1)
    service.clear(_INSTRUMENT, _CONTRACT)
    # Deliberately never start() the writer thread — nothing drains the queue, so a
    # second bar close overflows a maxsize=1 queue without any real I/O involved.
    _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)
    clock.advance(60 * 60)
    _push(service, bus, clock.now(), time(9, 50), "17600", serial_no=2)
    clock.advance(60 * 60)
    _push(service, bus, clock.now(), time(10, 50), "17700", serial_no=3)

    assert service.is_persistence_degraded() is True
    # The tick pipeline itself never raised or blocked — bars still closed normally.
    assert len(service.recent_closed_bars(_INSTRUMENT, _CONTRACT)) == 2
    health_events = [e for e in bus.published if isinstance(e, BarPersistenceHealthChanged)]
    assert health_events[-1].is_degraded is True


def test_persistent_write_failure_marks_degraded_then_recovers() -> None:
    repo = FakeBarRecordRepository()
    repo.fail_writes_remaining = 1
    service, bus, clock = _build(now=_ts(8, 50), bar_record_repository=repo, max_write_retries=1)
    service.clear(_INSTRUMENT, _CONTRACT)
    service.start()
    try:
        _push(service, bus, clock.now(), time(8, 50), "17500", serial_no=1)
        clock.advance(60 * 60)
        _push(service, bus, clock.now(), time(9, 50), "17600", serial_no=2)
        _wait_until(lambda: service.is_persistence_degraded() is True)

        clock.advance(60 * 60)
        _push(service, bus, clock.now(), time(10, 50), "17700", serial_no=3)
        _wait_until(lambda: service.is_persistence_degraded() is False)
    finally:
        service.stop()

    health_events = [e for e in bus.published if isinstance(e, BarPersistenceHealthChanged)]
    assert [e.is_degraded for e in health_events] == [True, False]


def test_clear_restores_streak_and_recent_bars_from_persisted_history() -> None:
    repo = FakeBarRecordRepository()
    bar1 = _insert_record(repo, _ts(8, 45), _ts(9, 45), open_="17500", close="17600")
    bar2 = _insert_record(repo, _ts(9, 45), _ts(10, 45), open_="17600", close="17700")
    service, _bus, _clock = _build(now=_ts(11, 0), bar_record_repository=repo)

    service.clear(_INSTRUMENT, _CONTRACT)

    assert list(service.recent_closed_bars(_INSTRUMENT, _CONTRACT)) == [bar1, bar2]
    color, length = service.candle_streak(_INSTRUMENT, _CONTRACT)
    assert color is CandleColor.RED
    assert length == 2


def test_broker_session_ready_reloads_persisted_history_on_reconnect() -> None:
    repo = FakeBarRecordRepository()
    service, bus, clock = _build(now=_ts(9, 0), bar_record_repository=repo)
    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.recent_closed_bars(_INSTRUMENT, _CONTRACT) == ()

    bar = _insert_record(repo, _ts(8, 45), _ts(9, 45), open_="17500", close="17600")
    bus.publish(BrokerSessionReady(at=clock.now(), account=_ACCOUNT))

    assert list(service.recent_closed_bars(_INSTRUMENT, _CONTRACT)) == [bar]


def test_first_activation_has_empty_history_and_never_queries_a_vendor() -> None:
    repo = FakeBarRecordRepository()
    service, _bus, _clock = _build(now=_ts(9, 0), bar_record_repository=repo)
    service.clear(_INSTRUMENT, _CONTRACT)

    assert service.recent_closed_bars(_INSTRUMENT, _CONTRACT) == ()
    assert service.recorded_range(_INSTRUMENT, _CONTRACT) is None
    assert service.continuous_warm_up_bars(_INSTRUMENT, _CONTRACT) == ()


def test_continuous_warm_up_bars_returns_only_the_tail_gap_free_segment() -> None:
    repo = FakeBarRecordRepository()
    _insert_record(repo, _ts(8, 45), _ts(9, 45), open_="17400", close="17450")
    # 09:45-10:45 is missing entirely — a genuine gap in the persisted history.
    bar1 = _insert_record(repo, _ts(10, 45), _ts(11, 45), open_="17500", close="17550")
    bar2 = _insert_record(repo, _ts(11, 45), _ts(12, 45), open_="17550", close="17600")
    service, _bus, _clock = _build(now=_ts(13, 0), bar_record_repository=repo)

    tail = service.continuous_warm_up_bars(_INSTRUMENT, _CONTRACT)
    assert list(tail) == [bar1, bar2]


def test_recorded_range_none_when_nothing_persisted() -> None:
    repo = FakeBarRecordRepository()
    service, _bus, _clock = _build(now=_ts(9, 0), bar_record_repository=repo)
    assert service.recorded_range(_INSTRUMENT, _CONTRACT) is None


def test_recorded_range_reports_incomplete_window_on_first_activation() -> None:
    repo = FakeBarRecordRepository()
    _insert_record(repo, _ts(8, 45), _ts(9, 45), open_="17500", close="17600")
    service, _bus, _clock = _build(now=_ts(9, 0), bar_record_repository=repo)

    recorded_range = service.recorded_range(_INSTRUMENT, _CONTRACT)
    assert recorded_range is not None
    assert recorded_range.covers_full_window is False


def test_query_history_delegates_to_repository_range() -> None:
    repo = FakeBarRecordRepository()
    bar = _insert_record(repo, _ts(8, 45), _ts(9, 45), open_="17500", close="17600")
    service, _bus, _clock = _build(now=_ts(9, 0), bar_record_repository=repo)

    records = service.query_history(
        _INSTRUMENT, _CONTRACT, start_date=_WEDNESDAY, end_date=_WEDNESDAY
    )
    assert [r.bar for r in records] == [bar]


def test_start_runs_retention_cleanup_and_purges_bars_older_than_two_months() -> None:
    repo = FakeBarRecordRepository()
    old = _insert_record(
        repo,
        _ts(8, 45, d=date(2026, 1, 5)),
        _ts(9, 45, d=date(2026, 1, 5)),
        open_="17000",
        close="17050",
    )
    recent = _insert_record(repo, _ts(8, 45), _ts(9, 45), open_="17500", close="17600")
    service, _bus, _clock = _build(now=_ts(9, 0), bar_record_repository=repo)

    service.start()
    try:
        _wait_until(lambda: len(repo.delete_before_calls) >= 1)
    finally:
        service.stop()

    remaining = repo.all_records()
    assert [r.bar.start for r in remaining] == [recent.start]
    assert old.start not in [r.bar.start for r in remaining]


def test_daily_rollover_triggers_retention_cleanup_via_on_clock_tick() -> None:
    repo = FakeBarRecordRepository()
    service, _bus, clock = _build(now=_ts(9, 0), bar_record_repository=repo)

    service.on_clock_tick()
    assert len(repo.delete_before_calls) == 1

    service.on_clock_tick()  # same day — no additional sweep
    assert len(repo.delete_before_calls) == 1

    clock.advance(24 * 60 * 60)
    service.on_clock_tick()
    assert len(repo.delete_before_calls) == 2


def test_retention_cleanup_publishes_a_completed_summary_event() -> None:
    repo = FakeBarRecordRepository()
    service, bus, _clock = _build(now=_ts(9, 0), bar_record_repository=repo)

    service.on_clock_tick()

    events = [e for e in bus.published if isinstance(e, BarRetentionCleanupCompleted)]
    assert len(events) == 1
    assert events[0].cutoff_trading_day == date(2026, 7, 16)
