from __future__ import annotations

import time as _real_time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import (
    BarBackfillCompleted,
    BrokerSessionReady,
    Event,
    InstrumentSwitchCompleted,
)
from tfx_quant.application.market_data.bar_history_backfill_service import (
    BarHistoryBackfillService,
)
from tfx_quant.application.ports.bar_record_repository import (
    BarRecordNotFoundError,
    BarUpsertOutcome,
    BarUpsertRepositoryError,
    RetentionCleanupSummary,
)
from tfx_quant.application.ports.yahoo_history_query import YahooBar, YahooHistoryQueryError
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import (
    BarConflictAudit,
    BarDataSource,
    BarPeriod,
    BarRecord,
    MarketSession,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_TARGET_DAY = date(2026, 9, 16)  # a Wednesday
_ACCOUNT = TradingAccount(branch_id="F00", account_no="9808900")
_TICKER = "TXF=F"


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

    def set(self, now: Timestamp) -> None:
        self._now = now


class FakeTradingCalendarRepository:
    def __init__(self, holidays: frozenset[date] = frozenset()) -> None:
        self._holidays = holidays

    def get_holidays(self) -> frozenset[date]:
        return self._holidays

    def get_early_closes(self) -> dict[date, time]:
        return {}


class FakeInstrumentMasterRepository:
    def __init__(self, entries: dict[tuple[Instrument, ContractMonth], InstrumentMasterEntry]):
        self._entries = entries

    def get(self, instrument: Instrument, contract: ContractMonth) -> InstrumentMasterEntry | None:
        return self._entries.get((instrument, contract))

    def list_for(self, instrument: Instrument) -> list[InstrumentMasterEntry]:
        return [e for (i, _c), e in self._entries.items() if i == instrument]


class FakeYahooTickerMappingRepository:
    def __init__(self, mapping: dict[tuple[Instrument, ContractMonth], str] | None = None) -> None:
        self._mapping = mapping or {}

    def get(self, instrument: Instrument, contract: ContractMonth) -> str | None:
        return self._mapping.get((instrument, contract))


class FakeYahooHistoryQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self._responder: Callable[[str, date, date], Sequence[YahooBar]] = lambda *_: ()

    def script(self, responder: Callable[[str, date, date], Sequence[YahooBar]]) -> None:
        self._responder = responder

    def query_1h_bars(
        self, *, yahoo_ticker: str, start_date: date, end_date: date
    ) -> Sequence[YahooBar]:
        self.calls.append((yahoo_ticker, start_date, end_date))
        return self._responder(yahoo_ticker, start_date, end_date)


class FakeBarRecordRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[Instrument, ContractMonth, BarPeriod, Timestamp], BarRecord] = {}
        self.conflicts: list[BarConflictAudit] = []
        self.fail_writes_remaining = 0

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
        if _ohlcv_matches(existing.bar, record.bar):
            return BarUpsertOutcome.DUPLICATE_IGNORED
        return BarUpsertOutcome.CONFLICT_REJECTED

    def apply_correction(self, record: BarRecord, *, reason: str) -> None:
        if record.identity not in self._rows:
            raise BarRecordNotFoundError(f"no existing row for {record.identity!r}")
        del reason
        self._rows[record.identity] = record

    def get_one(
        self, instrument: Instrument, contract: ContractMonth, period: BarPeriod, start: Timestamp
    ) -> BarRecord | None:
        return self._rows.get((instrument, contract, period, start))

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
        before = len(self._rows)
        self._rows = {k: v for k, v in self._rows.items() if v.trading_day >= cutoff_trading_day}
        return RetentionCleanupSummary(
            cutoff_trading_day=cutoff_trading_day,
            deleted_count=before - len(self._rows),
            ran_at=ran_at,
        )

    def record_conflict(self, audit: BarConflictAudit) -> None:
        self.conflicts.append(audit)

    def list_conflicted_trading_days(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        period: BarPeriod,
        *,
        since_trading_day: date,
    ) -> frozenset[date]:
        return frozenset(
            audit.incoming.trading_day
            for audit in self.conflicts
            if audit.incoming.bar.instrument == instrument
            and audit.incoming.bar.contract == contract
            and audit.incoming.period == period
            and audit.incoming.trading_day >= since_trading_day
        )


def _ohlcv_matches(a: Bar, b: Bar) -> bool:
    return (
        a.open.amount == b.open.amount
        and a.high.amount == b.high.amount
        and a.low.amount == b.low.amount
        and a.close.amount == b.close.amount
        and a.volume == b.volume
        and a.end.value == b.end.value
    )


def _entry(*, night_session: bool = False) -> InstrumentMasterEntry:
    return InstrumentMasterEntry(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        vendor_symbol="TXFU6",
        broker_product_code="TXF",
        tick_size=Decimal("1"),
        multiplier=Decimal("200"),
        day_session_start=time(8, 45),
        day_session_end=time(13, 45),
        night_session_start=time(15, 0) if night_session else None,
        night_session_end=time(5, 0) if night_session else None,
        expiry_date=date(2026, 9, 16),
        tradable=True,
    )


def _ts(hour: int, minute: int, d: date = _TARGET_DAY) -> Timestamp:
    return Timestamp(datetime(d.year, d.month, d.day, hour, minute, tzinfo=TAIPEI_TZ))


def _holidays_except(window_start: date, window_end: date, keep: date) -> frozenset[date]:
    """Marks every weekday in `[window_start, window_end]` other than `keep` as a
    holiday, so `TradingCalendar.trading_days_between` yields exactly `[keep]` —
    lets tests control the rolling two-month window's trading-day count precisely
    without needing a real multi-month holiday calendar."""
    days: set[date] = set()
    cursor = window_start
    while cursor <= window_end:
        if cursor.weekday() < 5 and cursor != keep:
            days.add(cursor)
        cursor += timedelta(days=1)
    return frozenset(days)


_WINDOW_START = date(2026, 9, 16)  # rolling_two_month_start(2026-11-16)
_WINDOW_END = date(2026, 11, 16)
_NOW = _ts(12, 0, d=_WINDOW_END)
_HOLIDAYS = _holidays_except(_WINDOW_START, _WINDOW_END, _TARGET_DAY)


def _build(
    *,
    bar_record_repository: FakeBarRecordRepository | None = None,
    ticker_mapping: FakeYahooTickerMappingRepository | None = None,
    yahoo_history_query: FakeYahooHistoryQuery | None = None,
    instrument_master: FakeInstrumentMasterRepository | None = None,
    now: Timestamp = _NOW,
    max_query_span_days: int = 10,
    gap_padding_days: int = 1,
) -> tuple[BarHistoryBackfillService, FakeEventBus, FakeClock, FakeBarRecordRepository]:
    bus = FakeEventBus()
    clock = FakeClock(now)
    repo = bar_record_repository or FakeBarRecordRepository()
    service = BarHistoryBackfillService(
        event_bus=bus,
        clock=clock,
        trading_calendar_repository=FakeTradingCalendarRepository(holidays=_HOLIDAYS),
        instrument_master=instrument_master
        or FakeInstrumentMasterRepository({(_INSTRUMENT, _CONTRACT): _entry()}),
        bar_record_repository=repo,
        yahoo_ticker_mapping=ticker_mapping or FakeYahooTickerMappingRepository(),
        yahoo_history_query=yahoo_history_query or FakeYahooHistoryQuery(),
        max_query_span_days=max_query_span_days,
        gap_padding_days=gap_padding_days,
    )
    return service, bus, clock, repo


def _yahoo_bar(hour: int, minute: int, *, price: str, d: date = _TARGET_DAY) -> YahooBar:
    at = datetime(d.year, d.month, d.day, hour, minute, tzinfo=TAIPEI_TZ)
    return YahooBar(
        at=at,
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=7,
    )


def _switch(bus: FakeEventBus) -> None:
    bus.publish(
        InstrumentSwitchCompleted(
            at=Timestamp.now(), instrument=_INSTRUMENT, contract=_CONTRACT, vendor_symbol="TXFU6"
        )
    )


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """Backfill runs happen on a background thread (see `BarHistoryBackfillService`'s
    module docstring) — every test that triggers a run must poll for its effect rather
    than assert immediately after publishing the triggering event."""
    deadline = _real_time.monotonic() + timeout
    while _real_time.monotonic() < deadline:
        if predicate():
            return True
        _real_time.sleep(0.01)
    return predicate()


def _wait_for_completed(bus: FakeEventBus, *, timeout: float = 2.0) -> BarBackfillCompleted:
    assert _wait_for(
        lambda: any(isinstance(e, BarBackfillCompleted) for e in bus.published), timeout=timeout
    ), "BarBackfillCompleted was never published"
    return next(e for e in bus.published if isinstance(e, BarBackfillCompleted))


# -- No gaps / no-op paths -----------------------------------------------------------


def test_publishes_completed_with_zero_requested_when_nothing_missing() -> None:
    # Fill every expected bar of the target day locally first.
    repo = FakeBarRecordRepository()
    for hour in (8, 9, 10, 11, 12):
        start = _ts(hour, 45)
        end = Timestamp(start.value + timedelta(hours=1))
        bar = Bar(
            instrument=_INSTRUMENT,
            contract=_CONTRACT,
            open=Price(Decimal("17500")),
            high=Price(Decimal("17500")),
            low=Price(Decimal("17500")),
            close=Price(Decimal("17500")),
            volume=1,
            start=start,
            end=end,
        )
        repo.upsert_closed_bar(
            BarRecord(
                bar=bar,
                period=BarPeriod.SIXTY_MINUTE,
                trading_day=_TARGET_DAY,
                session=MarketSession.DAY,
                source=BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME,
                is_gap_recovery=False,
                created_at=start,
                updated_at=start,
            )
        )
    query = FakeYahooHistoryQuery()
    service, bus, _clock, _repo = _build(bar_record_repository=repo, yahoo_history_query=query)

    _switch(bus)
    completed = _wait_for_completed(bus)

    assert query.calls == []
    assert completed.requested_bar_count == 0
    assert completed.filled_bar_count == 0


def test_missing_instrument_master_entry_is_skipped_without_crashing() -> None:
    service, bus, _clock, _repo = _build(instrument_master=FakeInstrumentMasterRepository({}))
    _switch(bus)  # must not raise
    # No master entry means the run returns immediately without ever publishing —
    # give the background thread a brief, bounded window to (not) do so.
    _real_time.sleep(0.05)
    assert [e for e in bus.published if isinstance(e, BarBackfillCompleted)] == []


# -- Missing ticker mapping -----------------------------------------------------------


def test_missing_ticker_mapping_leaves_gap_and_marks_degraded() -> None:
    query = FakeYahooHistoryQuery()
    service, bus, _clock, _repo = _build(yahoo_history_query=query)

    _switch(bus)
    completed = _wait_for_completed(bus)

    assert query.calls == []  # never guesses a ticker
    assert completed.requested_bar_count == 5  # 5 closed bars that day
    assert completed.filled_bar_count == 0
    assert service.is_degraded() is True


# -- Successful backfill --------------------------------------------------------------


def test_backfills_every_missing_bar_from_an_empty_local_history() -> None:
    query = FakeYahooHistoryQuery()
    query.script(
        lambda ticker, start, end: [
            _yahoo_bar(8, 45, price="17400"),
            _yahoo_bar(9, 45, price="17450"),
            _yahoo_bar(10, 45, price="17500"),
            _yahoo_bar(11, 45, price="17550"),
            _yahoo_bar(12, 45, price="17600"),
        ]
    )
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, _clock, repo = _build(ticker_mapping=mapping, yahoo_history_query=query)

    _switch(bus)
    completed = _wait_for_completed(bus)

    records = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date.min
    )
    assert len(records) == 5
    assert all(r.source is BarDataSource.BACKFILLED_FROM_YFINANCE for r in records)
    assert completed.requested_bar_count == 5
    assert completed.filled_bar_count == 5
    assert completed.conflict_count == 0
    assert service.is_degraded() is False


def test_second_run_is_idempotent_and_reports_nothing_new_missing() -> None:
    query = FakeYahooHistoryQuery()
    query.script(lambda ticker, start, end: [_yahoo_bar(8, 45, price="17400")])
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    repo = FakeBarRecordRepository()
    # First run fills the 08:45 bar (still leaves 09:45-12:45 missing since the
    # fake only ever returns the one bar).
    service, bus, _clock, _repo = _build(
        bar_record_repository=repo, ticker_mapping=mapping, yahoo_history_query=query
    )
    _switch(bus)
    _wait_for_completed(bus)
    first_filled = repo.list_recent(
        _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date.min
    )
    assert len(first_filled) == 1

    # Second run: 08:45 is no longer "missing" locally, so it's not counted as
    # newly filled again even though the fake still returns it (upsert dedups).
    bus.published.clear()
    _switch(bus)
    completed = _wait_for_completed(bus)
    assert completed.filled_bar_count == 0
    assert (
        len(
            repo.list_recent(
                _INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, since_trading_day=date.min
            )
        )
        == 1
    )  # no duplicate row


# -- Validation / boundary alignment ---------------------------------------------------


def test_off_grid_bar_is_dropped_not_written() -> None:
    query = FakeYahooHistoryQuery()
    query.script(lambda ticker, start, end: [_yahoo_bar(9, 0, price="17400")])  # not a boundary
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, _clock, repo = _build(ticker_mapping=mapping, yahoo_history_query=query)

    _switch(bus)
    completed = _wait_for_completed(bus)

    assert repo.all_records() == []
    assert completed.filled_bar_count == 0


def test_still_forming_bar_is_dropped_not_written() -> None:
    """A yfinance bar whose canonical close is still in the future relative to `now`
    must never be treated as a closed, backfillable bar."""
    query = FakeYahooHistoryQuery()
    # 12:45 boundary closes at 13:45; give it a `now` that lands inside that window.
    query.script(lambda ticker, start, end: [_yahoo_bar(12, 45, price="17600")])
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    early_now = _ts(13, 0, d=_TARGET_DAY)
    service, bus, _clock, repo = _build(
        ticker_mapping=mapping, yahoo_history_query=query, now=early_now
    )

    _switch(bus)
    _wait_for_completed(bus)

    assert repo.all_records() == []


def test_invalid_ohlcv_is_dropped_not_written() -> None:
    query = FakeYahooHistoryQuery()

    def responder(ticker: str, start: date, end: date) -> Sequence[YahooBar]:
        bar = _yahoo_bar(8, 45, price="17400")
        # high below low is nonsensical — domain Bar construction rejects it.
        return [
            YahooBar(
                at=bar.at,
                open=bar.open,
                high=Decimal("100"),
                low=Decimal("200"),
                close=bar.close,
                volume=1,
            )
        ]

    query.script(responder)
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, _clock, repo = _build(ticker_mapping=mapping, yahoo_history_query=query)

    _switch(bus)
    _wait_for_completed(bus)

    assert repo.all_records() == []


# -- Query failure handling -------------------------------------------------------------


def test_query_error_leaves_gap_without_crashing() -> None:
    query = FakeYahooHistoryQuery()

    def responder(ticker: str, start: date, end: date) -> Sequence[YahooBar]:
        raise YahooHistoryQueryError("simulated rate limit exhaustion")

    query.script(responder)
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, _clock, repo = _build(ticker_mapping=mapping, yahoo_history_query=query)

    _switch(bus)  # must not raise
    completed = _wait_for_completed(bus)

    assert repo.all_records() == []
    assert completed.filled_bar_count == 0


# -- Conflict handling --------------------------------------------------------------------


def test_conflict_with_existing_local_bar_is_never_overwritten_and_is_audited() -> None:
    repo = FakeBarRecordRepository()
    local_start = _ts(8, 45)
    local_end = Timestamp(local_start.value + timedelta(hours=1))
    local_bar = Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        open=Price(Decimal("17400")),
        high=Price(Decimal("17400")),
        low=Price(Decimal("17400")),
        close=Price(Decimal("17400")),
        volume=1,
        start=local_start,
        end=local_end,
    )
    repo.upsert_closed_bar(
        BarRecord(
            bar=local_bar,
            period=BarPeriod.SIXTY_MINUTE,
            trading_day=_TARGET_DAY,
            session=MarketSession.DAY,
            source=BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME,
            is_gap_recovery=False,
            created_at=local_start,
            updated_at=local_start,
        )
    )

    query = FakeYahooHistoryQuery()
    # Padding causes the already-covered 08:45 bar to be re-queried alongside the
    # genuinely missing 09:45 bar — yfinance disagrees on 08:45's price.
    query.script(
        lambda ticker, start, end: [
            _yahoo_bar(8, 45, price="17999"),
            _yahoo_bar(9, 45, price="17450"),
        ]
    )
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, _clock, _repo = _build(
        bar_record_repository=repo, ticker_mapping=mapping, yahoo_history_query=query
    )

    _switch(bus)
    completed = _wait_for_completed(bus)

    kept = repo.get_one(_INSTRUMENT, _CONTRACT, BarPeriod.SIXTY_MINUTE, local_start)
    assert kept is not None
    assert kept.bar.open.amount == Decimal("17400")  # local bar never overwritten
    assert len(repo.conflicts) == 1
    assert repo.conflicts[0].existing.bar.open.amount == Decimal("17400")
    assert repo.conflicts[0].incoming.bar.open.amount == Decimal("17999")
    assert service.is_degraded() is True

    assert completed.conflict_count == 1
    assert completed.filled_bar_count == 1  # only the genuinely-missing 09:45 bar


# -- Trigger wiring -------------------------------------------------------------------


def test_broker_session_ready_triggers_a_run_once_active_is_known() -> None:
    query = FakeYahooHistoryQuery()
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, _clock, _repo = _build(ticker_mapping=mapping, yahoo_history_query=query)

    # No active contract yet — BrokerSessionReady alone must not crash or run.
    bus.publish(BrokerSessionReady(at=Timestamp.now(), account=_ACCOUNT))
    _real_time.sleep(0.05)
    assert query.calls == []

    _switch(bus)
    _wait_for_completed(bus)
    assert query.calls != []
    calls_after_switch = len(query.calls)

    bus.published.clear()
    bus.publish(BrokerSessionReady(at=Timestamp.now(), account=_ACCOUNT))
    assert _wait_for(lambda: len(query.calls) > calls_after_switch)


def test_on_clock_tick_reruns_only_after_the_date_changes() -> None:
    query = FakeYahooHistoryQuery()
    mapping = FakeYahooTickerMappingRepository({(_INSTRUMENT, _CONTRACT): _TICKER})
    service, bus, clock, _repo = _build(ticker_mapping=mapping, yahoo_history_query=query)
    _switch(bus)
    _wait_for_completed(bus)
    calls_after_switch = len(query.calls)

    service.on_clock_tick()  # same date — decided synchronously, no rerun spawned
    _real_time.sleep(0.05)
    assert len(query.calls) == calls_after_switch

    clock.set(Timestamp(clock.now().value + timedelta(days=1)))
    service.on_clock_tick()
    assert _wait_for(lambda: len(query.calls) > calls_after_switch)


def test_start_and_stop_do_not_raise() -> None:
    service, _bus, _clock, _repo = _build()
    service.start()
    service.start()  # idempotent
    service.stop()
    service.stop()  # idempotent
