from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from pydantic import SecretStr

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.events.events import (
    BarClosed,
    Event,
    InstrumentSwitchCompleted,
    LatestPriceObserved,
    MarketDataFreshnessChanged,
)
from tfx_quant.application.instrument_selection.selection import ResolvedSelection
from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.desktop.quote_runtime import QuoteRuntime
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.persistence.sqlite_bar_record_repository import SqliteBarRecordRepository
from tfx_quant.persistence.sqlite_market_event_repository import SqliteMarketEventRepository

_CONTRACT = ContractMonth(2026, 9)
_SYMBOLS = {Instrument.TXF: "TXFI6", Instrument.MXF: "MXFI6"}


def _entry(instrument: Instrument, contract: ContractMonth = _CONTRACT) -> InstrumentMasterEntry:
    return InstrumentMasterEntry(
        instrument=instrument,
        contract=contract,
        vendor_symbol=_SYMBOLS[instrument] if contract == _CONTRACT else "TXFL6",
        broker_product_code=instrument.value,
        tick_size=Decimal("1"),
        multiplier=Decimal("200"),
        day_session_start=time(8, 45),
        day_session_end=time(13, 45),
        night_session_start=time(15, 0),
        night_session_end=time(5, 0),
        expiry_date=date(2026, 9, 16),
        tradable=True,
    )


class _SyncBus(EventCoordinator):
    """Real subscription semantics, dispatched on the publishing thread so a test can
    assert straight after the call instead of waiting on the consumer thread."""

    def publish(self, event: Event) -> None:
        self._dispatch(event)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> Timestamp:
        return Timestamp(self.value)


class _Selection:
    """Only the two members `QuoteRuntime` reads off the selection service."""

    def __init__(self, instrument: Instrument) -> None:
        self.current: ResolvedSelection | None = ResolvedSelection(
            instrument, _CONTRACT, _entry(instrument)
        )

    def resolve_near_month(self, instrument: Instrument) -> ResolvedSelection:
        return ResolvedSelection(instrument, _CONTRACT, _entry(instrument))


class _Master:
    def get(self, instrument: Instrument, contract: ContractMonth) -> InstrumentMasterEntry | None:
        return _entry(instrument, contract)

    def list_for(self, instrument: Instrument) -> Sequence[InstrumentMasterEntry]:
        return [_entry(instrument)]


class _Calendar:
    def get_holidays(self) -> frozenset[date]:
        return frozenset()

    def get_early_closes(self) -> dict[date, time]:
        return {}


class _Gateway:
    def __init__(
        self,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
    ) -> None:
        self.on_event, self.on_gap = on_event, on_gap
        self.state = QuoteConnectionState.IDLE
        self.subscriptions: list[str] = []
        self.unsubscriptions: list[str] = []

    def connect(self, *_args: object, **_kwargs: object) -> None:
        self.state = QuoteConnectionState.LOGGED_ON

    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None:
        del request_type, mode
        self.subscriptions.append(symbol)

    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None:
        del request_type
        self.unsubscriptions.append(symbol)

    def stop(self) -> None:
        self.state = QuoteConnectionState.STOPPED


def _trade(symbol: str, sequence: int, at: datetime, *, price: str, total: int) -> RawMarketEvent:
    return RawMarketEvent(
        symbol,
        sequence,
        "session-1",
        Timestamp(at),
        {
            "MatchTime": at.strftime("%H%M%S") + f"{at.microsecond:06d}",
            "MatchPri": price,
            "MatchQty": "1",
            "TolMatchQty": str(total),
        },
    )


class _Harness:
    def __init__(self, now: datetime, charted: Instrument = Instrument.MXF) -> None:
        self.clock = _Clock(now)
        self.bus = _SyncBus()
        self.selection = _Selection(charted)
        self.bars = SqliteBarRecordRepository(sqlite3.connect(":memory:", check_same_thread=False))
        self.gateways: list[_Gateway] = []
        self.closed: list[BarClosed] = []
        self.bus.subscribe(BarClosed, self.closed.append)
        self.runtime = QuoteRuntime(
            clock=self.clock,  # type: ignore[arg-type]
            event_bus=self.bus,
            selection=self.selection,  # type: ignore[arg-type]
            instrument_master=_Master(),  # type: ignore[arg-type]
            trading_calendar=_Calendar(),  # type: ignore[arg-type]
            bar_repository=self.bars,
            event_repository=SqliteMarketEventRepository(sqlite3.connect(":memory:")),
            gateway_factory=self._factory,
        )

    def _factory(
        self,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
    ) -> _Gateway:
        gateway = _Gateway(on_event, on_gap)
        self.gateways.append(gateway)
        return gateway

    def login(self) -> None:
        self.runtime.start("A123456789", SecretStr("secret"))

    def switch_to(self, instrument: Instrument) -> None:
        self.selection.current = ResolvedSelection(instrument, _CONTRACT, _entry(instrument))
        self.bus.publish(
            InstrumentSwitchCompleted(
                at=self.clock.now(),
                instrument=instrument,
                contract=_CONTRACT,
                vendor_symbol=_SYMBOLS[instrument],
            )
        )


def test_login_registers_and_records_both_markets_at_once() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()

    assert sorted(harness.gateways[0].subscriptions) == ["MXFI6", "TXFI6"]

    for index, symbol in enumerate(("MXFI6", "TXFI6")):
        gateway = harness.gateways[0]
        gateway.on_event(
            _trade(
                symbol,
                index * 2 + 1,
                datetime(2026, 9, 1, 10, 0, tzinfo=TAIPEI_TZ),
                price="18000",
                total=10,
            )
        )
        gateway.on_event(
            _trade(
                symbol,
                index * 2 + 2,
                datetime(2026, 9, 1, 10, 30, tzinfo=TAIPEI_TZ),
                price="18010",
                total=20,
            )
        )
    harness.clock.value = datetime(2026, 9, 1, 10, 45, tzinfo=TAIPEI_TZ)
    harness.runtime.refresh()

    # 大台指 is recorded even though 小台指 is the charted market.
    assert sorted((event.instrument, event.bar.close.amount) for event in harness.closed) == [
        (Instrument.MXF, Decimal("18010")),
        (Instrument.TXF, Decimal("18010")),
    ]


def test_switching_the_charted_market_leaves_both_recordings_running() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    gateway = harness.gateways[0]
    gateway.on_event(
        _trade("TXFI6", 1, datetime(2026, 9, 1, 10, 0, tzinfo=TAIPEI_TZ), price="18000", total=10)
    )

    harness.switch_to(Instrument.TXF)

    # No re-registration, and the 大台指 bar that was already forming survives intact.
    assert gateway.unsubscriptions == []
    assert sorted(gateway.subscriptions) == ["MXFI6", "TXFI6"]
    forming = harness.runtime.forming_bar
    assert forming is not None
    assert forming.instrument is Instrument.TXF
    assert forming.open.amount == Decimal("18000")


def test_the_charted_market_selects_which_recorded_stream_the_chart_shows() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    harness.gateways[0].on_event(
        _trade("TXFI6", 1, datetime(2026, 9, 1, 10, 0, tzinfo=TAIPEI_TZ), price="18000", total=10)
    )

    # 大台指 is recording, but 小台指 is charted and has seen no trade of its own.
    assert harness.runtime.forming_bar is None


def test_boundary_under_way_at_login_is_neither_drawn_nor_persisted() -> None:
    harness = _Harness(datetime(2026, 9, 1, 10, 12, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    gateway = harness.gateways[0]
    for sequence, minute in enumerate((20, 30, 40), start=1):
        gateway.on_event(
            _trade(
                "MXFI6",
                sequence,
                datetime(2026, 9, 1, 10, minute, tzinfo=TAIPEI_TZ),
                price="18000",
                total=sequence * 10,
            )
        )

    # 09:45-10:45 was already half over at login: nothing to draw...
    assert harness.runtime.forming_bar is None

    harness.clock.value = datetime(2026, 9, 1, 10, 46, tzinfo=TAIPEI_TZ)
    harness.runtime.refresh()

    # ...and nothing persisted or published when that boundary passes.
    assert harness.closed == []
    assert harness.runtime.query(date(2026, 9, 1), date(2026, 9, 1)) == []


def test_login_exactly_on_an_open_label_records_that_boundary() -> None:
    harness = _Harness(datetime(2026, 9, 1, 10, 45, 0, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    harness.gateways[0].on_event(
        _trade("MXFI6", 1, datetime(2026, 9, 1, 10, 45, tzinfo=TAIPEI_TZ), price="18000", total=10)
    )

    forming = harness.runtime.forming_bar
    assert forming is not None
    assert forming.start.value == datetime(2026, 9, 1, 10, 45, tzinfo=TAIPEI_TZ)


def test_a_gap_marks_only_its_own_symbols_stream_incomplete() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    gateway = harness.gateways[0]
    for index, symbol in enumerate(("MXFI6", "TXFI6")):
        gateway.on_event(
            _trade(
                symbol,
                index + 1,
                datetime(2026, 9, 1, 10, 0, tzinfo=TAIPEI_TZ),
                price="18000",
                total=10,
            )
        )

    gateway.on_gap(MarketDataGap("TXFI6", Timestamp(harness.clock.value), None, "disconnect"))
    harness.clock.value = datetime(2026, 9, 1, 10, 45, tzinfo=TAIPEI_TZ)
    harness.runtime.refresh()

    assert [event.instrument for event in harness.closed] == [Instrument.MXF]


def test_a_stopped_and_restarted_session_drops_the_boundary_it_restarts_inside() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    harness.gateways[0].on_event(
        _trade("MXFI6", 1, datetime(2026, 9, 1, 9, 50, tzinfo=TAIPEI_TZ), price="18000", total=10)
    )
    harness.runtime.stop()

    harness.clock.value = datetime(2026, 9, 1, 10, 5, tzinfo=TAIPEI_TZ)
    harness.login()
    harness.gateways[-1].on_event(
        _trade("MXFI6", 2, datetime(2026, 9, 1, 10, 10, tzinfo=TAIPEI_TZ), price="18020", total=20)
    )
    harness.clock.value = datetime(2026, 9, 1, 10, 46, tzinfo=TAIPEI_TZ)
    harness.runtime.refresh()

    assert harness.closed == []


def test_no_trade_after_login_leaves_the_recorded_history_empty() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    harness.clock.value = datetime(2026, 9, 1, 11, 0, tzinfo=TAIPEI_TZ)
    harness.runtime.refresh()

    assert harness.closed == []
    assert harness.runtime.query(date(2026, 9, 1), date(2026, 9, 1)) == []


def test_refresh_reports_staleness_for_every_recorded_market() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    freshness: list[MarketDataFreshnessChanged] = []
    harness.bus.subscribe(MarketDataFreshnessChanged, freshness.append)
    harness.login()
    harness.runtime.refresh()

    assert sorted(event.instrument.value for event in freshness) == ["MXF", "TXF"]
    assert {event.is_stale for event in freshness} == {False}


def test_latest_price_is_published_coalesced_to_one_per_second_per_market() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    prices: list[LatestPriceObserved] = []
    harness.bus.subscribe(LatestPriceObserved, prices.append)
    harness.login()
    gateway = harness.gateways[0]

    base = datetime(2026, 9, 1, 10, 0, 0, tzinfo=TAIPEI_TZ)
    # five MXF trades inside one second, then one a second later
    for i in range(5):
        gateway.on_event(
            _trade(
                "MXFI6",
                i + 1,
                base.replace(microsecond=i * 100_000),
                price=str(18000 + i),
                total=(i + 1) * 10,
            )
        )
    gateway.on_event(_trade("MXFI6", 6, base + timedelta(seconds=1), price="18100", total=100))

    mxf_prices = [p for p in prices if p.instrument is Instrument.MXF]
    assert [str(p.price) for p in mxf_prices] == ["18000", "18100"]
    assert mxf_prices[0].quality == "OK"
    assert harness.runtime.coalesced_price_updates == 4


def test_latest_price_quality_is_gap_after_a_gap_on_that_market() -> None:
    harness = _Harness(datetime(2026, 9, 1, 9, 45, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    prices: list[LatestPriceObserved] = []
    harness.bus.subscribe(LatestPriceObserved, prices.append)
    harness.login()
    gateway = harness.gateways[0]
    gateway.on_gap(MarketDataGap("MXFI6", Timestamp(harness.clock.value), None, "disconnect"))
    gateway.on_event(
        _trade("MXFI6", 1, datetime(2026, 9, 1, 10, 0, tzinfo=TAIPEI_TZ), price="18000", total=10)
    )

    assert [p.quality for p in prices if p.instrument is Instrument.MXF] == ["GAP"]


def test_recording_survives_a_night_session_reconnect_without_losing_a_market() -> None:
    harness = _Harness(datetime(2026, 9, 1, 16, 30, tzinfo=TAIPEI_TZ), charted=Instrument.MXF)
    harness.login()
    assert sorted(harness.gateways[0].subscriptions) == ["MXFI6", "TXFI6"]

    harness.clock.value = harness.clock.value + timedelta(hours=1)
    harness.runtime.refresh()

    assert sorted(harness.gateways[0].subscriptions) == ["MXFI6", "TXFI6"]
