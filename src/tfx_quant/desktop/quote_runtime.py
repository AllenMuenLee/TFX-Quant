"""Desktop composition facade for live Yuanta quotes and local bar history."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from pydantic import SecretStr

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.events.events import (
    BarClosed,
    InstrumentSwitchCompleted,
    MarketDataFreshnessChanged,
    MarketDataGapDetected,
)
from tfx_quant.application.instrument_selection.instrument_selection_service import (
    InstrumentSelectionService,
)
from tfx_quant.application.market_data.closed_bar_writer import LocalClosedBarWriter
from tfx_quant.application.market_data.event_parser import MarketEventParser
from tfx_quant.application.market_data.live_quote_service import LiveQuoteService
from tfx_quant.application.market_data.quote_session import parse_match_time
from tfx_quant.application.market_data.realtime_bar_aggregator import (
    ClosedAggregation,
    RealtimeBarAggregator,
)
from tfx_quant.application.market_data.recorder_service import MarketDataRecorderService
from tfx_quant.application.ports.bar_record_repository import BarRecordRepository
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.market_event_repository import MarketEventRepository
from tfx_quant.application.ports.quote_gateway import QuoteConnectionState, QuoteGateway
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import BarPeriod, BarRecord, rolling_two_month_start
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)


class QuoteRuntime:
    def __init__(
        self,
        *,
        clock: Clock,
        event_bus: EventCoordinator,
        selection: InstrumentSelectionService,
        instrument_master: InstrumentMasterRepository,
        trading_calendar: TradingCalendarRepository,
        bar_repository: BarRecordRepository,
        event_repository: MarketEventRepository,
        gateway_factory: Callable[
            [Callable[[RawMarketEvent], None], Callable[[MarketDataGap], None]], QuoteGateway
        ],
    ) -> None:
        self._clock, self._bus, self._selection = clock, event_bus, selection
        self._master, self._bars, self._events = instrument_master, bar_repository, event_repository
        self._calendar = TradingCalendar(
            trading_calendar.get_holidays(), trading_calendar.get_early_closes()
        )
        self._aggregator: RealtimeBarAggregator | None = None
        self._recorder: MarketDataRecorderService | None = None
        self._last_cleanup_date: date | None = None
        self._last_stale: bool | None = None
        self._last_event_at: Timestamp | None = None
        self._event_count = 0
        self._live = LiveQuoteService(lambda: gateway_factory(self._on_event, self._on_gap), clock)
        event_bus.subscribe(InstrumentSwitchCompleted, self._on_switch)

    @property
    def state(self) -> QuoteConnectionState:
        return self._live.state

    @property
    def last_event_at(self) -> Timestamp | None:
        """When the most recent ``OnGetMktAll`` callback of this run was received."""
        return self._last_event_at

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def forming_bar(self) -> Bar | None:
        return None if self._aggregator is None else self._aggregator.forming_bar

    def start(self, user_id: str, password: SecretStr) -> None:
        current = self._selection.current
        if current is None:
            raise RuntimeError("select a contract before quote login")
        self._configure(current.instrument, current.contract)
        self._live.start(user_id, password, current.entry.vendor_symbol)

    def refresh(self) -> None:
        today = self._clock.now().value.date()
        if self._last_cleanup_date != today:
            self._bars.delete_before(rolling_two_month_start(today), ran_at=self._clock.now())
            self._last_cleanup_date = today
        if self._recorder is not None:
            self._recorder.advance(self._clock.now())
        self._live.refresh()
        current = self._selection.current
        stale = self._live.state is not QuoteConnectionState.LOGGED_ON
        if current is not None and stale != self._last_stale:
            log_info(
                _logger,
                "market_data_freshness_changed",
                is_stale=stale,
                quote_state=self._live.state.value,
                event_count=self._event_count,
                last_event_at=(
                    None if self._last_event_at is None else self._last_event_at.value.isoformat()
                ),
            )
            self._bus.publish(
                MarketDataFreshnessChanged(
                    at=self._clock.now(),
                    instrument=current.instrument,
                    contract=current.contract,
                    is_stale=stale,
                )
            )
            self._last_stale = stale

    def stop(self) -> None:
        self._live.stop()

    def query(self, start: date, end: date) -> list[BarRecord]:
        current = self._selection.current
        if current is None:
            return []
        return list(
            self._bars.query_range(
                current.instrument,
                current.contract,
                BarPeriod.SIXTY_MINUTE,
                start_date=start,
                end_date=end,
            )
        )

    def _on_switch(self, event: InstrumentSwitchCompleted) -> None:
        self._configure(event.instrument, event.contract)
        self._live.select_symbol(event.vendor_symbol)

    def _configure(self, instrument: Instrument, contract: ContractMonth) -> None:
        entry = self._master.get(instrument, contract)
        if entry is None:
            raise RuntimeError("selected contract is missing from instrument master")
        aggregator = RealtimeBarAggregator(
            instrument, contract, lambda at: self._calendar.boundary_containing(at, entry)
        )
        writer = LocalClosedBarWriter(
            self._bars,
            self._clock,
            lambda record: self._bus.publish(
                BarClosed(
                    at=self._clock.now(),
                    instrument=instrument,
                    contract=contract,
                    bar=record.bar,
                )
            ),
        )
        self._aggregator = aggregator

        def persist(closed: ClosedAggregation) -> None:
            writer.persist(closed)

        self._recorder = MarketDataRecorderService(
            self._events, MarketEventParser(parse_match_time), aggregator, persist
        )

    def _on_event(self, event: RawMarketEvent) -> None:
        self._last_event_at = event.received_at
        self._event_count += 1
        if self._recorder is not None:
            self._recorder.record(event)

    def _on_gap(self, gap: MarketDataGap) -> None:
        log_warning(
            _logger,
            "market_data_gap_detected",
            symbol=gap.symbol,
            reason=gap.reason,
            started_at=gap.start.value.isoformat(),
        )
        self._events.record_gap(gap)
        if self._aggregator is not None:
            self._aggregator.mark_incomplete()
        current = self._selection.current
        if current is not None:
            self._bus.publish(
                MarketDataGapDetected(
                    at=self._clock.now(),
                    instrument=current.instrument,
                    contract=current.contract,
                    reason=gap.reason,
                )
            )
