from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import (
    BarClosed,
    BrokerSessionReady,
    Event,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
    MarketDataTickReceived,
)
from tfx_quant.application.market_data.bar_service import MarketDataBarService
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
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

    def get(
        self, instrument: Instrument, contract: ContractMonth
    ) -> InstrumentMasterEntry | None:
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


def _build(
    *, now: Timestamp | None = None, stale_after_seconds: float = 10.0
) -> tuple[MarketDataBarService, FakeEventBus, FakeClock]:
    bus = FakeEventBus()
    clock = FakeClock(now or _ts(8, 40))
    service = MarketDataBarService(
        event_bus=bus,
        clock=clock,
        trading_calendar_repository=FakeTradingCalendarRepository(),
        instrument_master=FakeInstrumentMasterRepository({(_INSTRUMENT, _CONTRACT): _entry()}),
        stale_after_seconds=stale_after_seconds,
    )
    return service, bus, clock


def _push(
    service: MarketDataBarService,
    bus: FakeEventBus,
    at: Timestamp,
    exchange_time: time,
    price: str,
    cumulative_volume: int,
    size: int = 1,
    vendor_symbol: str = _VENDOR_SYMBOL,
) -> None:
    bus.publish(
        MarketDataTickReceived(
            at=at,
            vendor_symbol=vendor_symbol,
            price=Decimal(price),
            size=size,
            cumulative_volume=cumulative_volume,
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
    _push(service, bus, clock.now(), time(9, 0), "17500", cumulative_volume=1)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None


def test_tick_with_symbol_mismatch_is_ignored() -> None:
    service, bus, clock = _build()
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(
        service, bus, clock.now(), time(9, 0), "17500", cumulative_volume=1,
        vendor_symbol="SOMETHING-ELSE",
    )
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None


def test_tick_updates_forming_bar_and_clears_staleness() -> None:
    service, bus, clock = _build(now=_ts(9, 0))
    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is True  # nothing received yet

    _push(service, bus, clock.now(), time(9, 0), "17500", cumulative_volume=1)

    forming = service.forming_bar(_INSTRUMENT, _CONTRACT)
    assert forming is not None
    assert forming.open.amount == Decimal("17500")
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is False
    freshness_events = [e for e in bus.published if isinstance(e, MarketDataFreshnessChanged)]
    assert freshness_events[-1].is_stale is False


def test_bar_closed_published_when_boundary_crossed() -> None:
    service, bus, clock = _build(now=_ts(8, 50))
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(8, 50), "17500", cumulative_volume=1)

    clock.advance(60 * 60)  # 09:50
    _push(service, bus, clock.now(), time(9, 50), "17600", cumulative_volume=2)

    closed_events = [e for e in bus.published if isinstance(e, BarClosed)]
    assert len(closed_events) == 1
    assert closed_events[0].bar.start.value.time() == time(8, 45)
    assert list(service.recent_closed_bars(_INSTRUMENT, _CONTRACT)) == [closed_events[0].bar]


def test_on_clock_tick_marks_stale_after_threshold_with_no_new_ticks() -> None:
    service, bus, clock = _build(now=_ts(9, 0), stale_after_seconds=5.0)
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(9, 0), "17500", cumulative_volume=1)
    assert service.is_stale(_INSTRUMENT, _CONTRACT) is False

    clock.advance(10.0)
    service.on_clock_tick()

    assert service.is_stale(_INSTRUMENT, _CONTRACT) is True
    freshness_events = [e for e in bus.published if isinstance(e, MarketDataFreshnessChanged)]
    assert freshness_events[-1].is_stale is True


def test_on_clock_tick_closes_forming_bar_with_no_new_tick() -> None:
    service, bus, clock = _build(now=_ts(8, 50))
    service.clear(_INSTRUMENT, _CONTRACT)
    _push(service, bus, clock.now(), time(8, 50), "17500", cumulative_volume=1)

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

    _push(service, bus, clock.now(), time(8, 50), "17500", cumulative_volume=1)
    clock.advance(60 * 60)
    _push(service, bus, clock.now(), time(9, 50), "17600", cumulative_volume=2)

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
    _push(service, bus, clock.now(), time(8, 50), "17500", cumulative_volume=1)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is not None

    service.clear(_INSTRUMENT, _CONTRACT)
    assert service.forming_bar(_INSTRUMENT, _CONTRACT) is None
