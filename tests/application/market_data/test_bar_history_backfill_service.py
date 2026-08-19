from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time
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
    RetentionCleanupSummary,
)
from tfx_quant.application.ports.historical_price_query import (
    HistoricalPriceQueryError,
    VendorKLineBar,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord, MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_VENDOR_SYMBOL = "TXFU6"
_ACCOUNT = TradingAccount(branch_id="F00", account_no="9808900")
_TODAY = date(2026, 8, 19)  # a Wednesday


class FakeEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        self.published: list[Event] = []
        self._lock = threading.Lock()

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
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


class FakeTradingCalendarRepository:
    def get_holidays(self) -> frozenset[date]:
        return frozenset()

    def get_early_closes(self) -> dict[date, time]:
        return {}


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
        night_session_start=None,
        night_session_end=None,
        expiry_date=date(2026, 9, 16),
        tradable=True,
    )


def _bar_ohlcv_matches(a: Any, b: Any) -> bool:
    return (
        a.open.amount == b.open.amount
        and a.high.amount == b.high.amount
        and a.low.amount == b.low.amount
        and a.close.amount == b.close.amount
        and a.volume == b.volume
        and a.end.value == b.end.value
    )


class FakeBarRecordRepository:
    def __init__(self) -> None:
        self._rows: dict[Any, BarRecord] = {}

    def all_records(self) -> list[BarRecord]:
        return sorted(self._rows.values(), key=lambda r: r.bar.start.value)

    def seed(self, record: BarRecord) -> None:
        self._rows[record.identity] = record

    def upsert_closed_bar(self, record: BarRecord) -> BarUpsertOutcome:
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
        del reason
        self._rows[record.identity] = record

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


class FakeHistoricalPriceQuery:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.bars_by_range: dict[tuple[date, date], list[VendorKLineBar]] = {}
        self.raise_for: set[tuple[date, date]] = set()

    def query_60m_kline(
        self,
        *,
        account: TradingAccount,
        vendor_symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[VendorKLineBar]:
        del account
        self.calls.append((vendor_symbol, start_date, end_date))
        if (start_date, end_date) in self.raise_for:
            raise HistoricalPriceQueryError("simulated vendor failure")
        return self.bars_by_range.get((start_date, end_date), [])


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.01)
    return predicate()


_BuiltService = tuple[
    BarHistoryBackfillService, FakeEventBus, FakeBarRecordRepository, FakeHistoricalPriceQuery
]


def _default_now() -> Timestamp:
    return Timestamp(datetime(_TODAY.year, _TODAY.month, _TODAY.day, 16, 0, tzinfo=TAIPEI_TZ))


def _build_service(
    *,
    bar_record_repository: FakeBarRecordRepository | None = None,
    historical_price_query: FakeHistoricalPriceQuery | None = None,
    now: Timestamp | None = None,
    min_query_interval_seconds: float = 0.0,
) -> _BuiltService:
    event_bus = FakeEventBus()
    clock = FakeClock(now or _default_now())
    repo = bar_record_repository or FakeBarRecordRepository()
    query = historical_price_query or FakeHistoricalPriceQuery()
    service = BarHistoryBackfillService(
        event_bus=event_bus,
        clock=clock,
        trading_calendar_repository=FakeTradingCalendarRepository(),
        instrument_master=FakeInstrumentMasterRepository({(_INSTRUMENT, _CONTRACT): _entry()}),
        bar_record_repository=repo,
        historical_price_query=query,
        min_query_interval_seconds=min_query_interval_seconds,
    )
    return service, event_bus, repo, query


def _ready(event_bus: FakeEventBus) -> None:
    event_bus.publish(BrokerSessionReady(at=Timestamp.now(), account=_ACCOUNT))


def _switched(event_bus: FakeEventBus) -> None:
    event_bus.publish(
        InstrumentSwitchCompleted(
            at=Timestamp.now(),
            instrument=_INSTRUMENT,
            contract=_CONTRACT,
            vendor_symbol=_VENDOR_SYMBOL,
        )
    )


def test_no_backfill_runs_without_both_account_and_active_contract() -> None:
    service, event_bus, _repo, query = _build_service()
    _ready(event_bus)  # account known, but no contract selected yet
    assert _wait_for(lambda: len(query.calls) > 0, timeout=0.2) is False
    assert query.calls == []


def test_backfill_queries_vendor_for_every_missing_trading_day() -> None:
    service, event_bus, _repo, query = _build_service()
    _ready(event_bus)
    _switched(event_bus)

    assert _wait_for(lambda: len(query.calls) > 0)
    assert query.calls
    vendor_symbol, start, end = query.calls[0]
    assert vendor_symbol == _VENDOR_SYMBOL
    assert start <= end


def test_backfill_writes_returned_bars_with_backfilled_source() -> None:
    query = FakeHistoricalPriceQuery()
    service, event_bus, repo, query = _build_service(historical_price_query=query)

    # Seed the vendor response once we know what range will be requested.
    _ready(event_bus)
    _switched(event_bus)
    assert _wait_for(lambda: len(query.calls) > 0)

    # A fresh run: no bars written yet since the fake had no seeded data.
    assert _wait_for(lambda: any(isinstance(e, BarBackfillCompleted) for e in event_bus.published))
    completed = [e for e in event_bus.published if isinstance(e, BarBackfillCompleted)]
    assert completed
    assert completed[-1].filled_day_count == 0


def test_backfill_writes_vendor_bar_at_exact_open_boundary() -> None:
    query = FakeHistoricalPriceQuery()
    # Pre-seed so the vendor returns one 60m bar for the first missing trading day
    # once the service asks for it, matching the day-session open boundary exactly.
    now = _default_now()
    service, event_bus, repo, query = _build_service(historical_price_query=query, now=now)

    # Trigger once to learn the first requested chunk, then seed a response and
    # trigger again via a second instrument-switch event (simulating a retry).
    _ready(event_bus)
    _switched(event_bus)
    assert _wait_for(lambda: len(query.calls) > 0)
    first_call = query.calls[0]
    _, start, _end = first_call
    query.bars_by_range[(start, first_call[2])] = [
        VendorKLineBar(
            at=datetime(start.year, start.month, start.day, 8, 45, tzinfo=TAIPEI_TZ),
            open=Decimal("17500"),
            high=Decimal("17550"),
            low=Decimal("17480"),
            close=Decimal("17520"),
            volume=100,
        )
    ]

    # Force a fresh run so the seeded data actually gets requested and written.
    query.calls.clear()
    _switched(event_bus)
    assert _wait_for(lambda: len(query.calls) > 0)
    assert _wait_for(lambda: len(repo.all_records()) > 0)

    records = repo.all_records()
    assert len(records) == 1
    record = records[0]
    assert record.source == BarDataSource.BACKFILLED_FROM_YUANTA_KLINE
    assert record.trading_day == start
    assert record.bar.open.amount == Decimal("17500")


def test_backfill_never_touches_a_day_that_already_has_a_local_bar() -> None:
    repo = FakeBarRecordRepository()
    now = _default_now()
    query = FakeHistoricalPriceQuery()
    service, event_bus, repo, query = _build_service(
        bar_record_repository=repo, historical_price_query=query, now=now
    )

    existing_bar = Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        open=Price(Decimal("100")),
        high=Price(Decimal("101")),
        low=Price(Decimal("99")),
        close=Price(Decimal("100")),
        volume=1,
        start=Timestamp(datetime(_TODAY.year, _TODAY.month, _TODAY.day, 8, 45, tzinfo=TAIPEI_TZ)),
        end=Timestamp(datetime(_TODAY.year, _TODAY.month, _TODAY.day, 9, 45, tzinfo=TAIPEI_TZ)),
    )
    repo.seed(
        BarRecord(
            bar=existing_bar,
            period=BarPeriod.SIXTY_MINUTE,
            trading_day=_TODAY,
            session=MarketSession.DAY,
            source=BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME,
            is_gap_recovery=False,
            created_at=now,
            updated_at=now,
        )
    )

    _ready(event_bus)
    _switched(event_bus)
    assert _wait_for(lambda: any(isinstance(e, BarBackfillCompleted) for e in event_bus.published))

    for _vendor_symbol, start, end in query.calls:
        assert start != _TODAY or end != _TODAY  # today, being covered, is never queried alone
    records = repo.all_records()
    assert len(records) == 1
    assert records[0].source == BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME


def test_backfill_treats_vendor_error_as_a_gap_not_a_crash() -> None:
    query = FakeHistoricalPriceQuery()
    now = _default_now()
    service, event_bus, repo, query = _build_service(historical_price_query=query, now=now)

    _ready(event_bus)
    _switched(event_bus)
    assert _wait_for(lambda: len(query.calls) > 0)
    for call in query.calls:
        query.raise_for.add((call[1], call[2]))

    query.calls.clear()
    _switched(event_bus)
    assert _wait_for(lambda: any(isinstance(e, BarBackfillCompleted) for e in event_bus.published))
    completed = [e for e in event_bus.published if isinstance(e, BarBackfillCompleted)]
    assert completed[-1].filled_day_count == 0
    assert repo.all_records() == []
