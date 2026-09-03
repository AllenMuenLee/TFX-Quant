"""Desktop composition facade for live Yuanta quotes and local bar history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from pydantic import SecretStr

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.events.events import (
    BarClosed,
    InstrumentSwitchCompleted,
    LatestPriceObserved,
    MarketDataFreshnessChanged,
    MarketDataGapDetected,
)
from tfx_quant.application.instrument_selection.errors import InstrumentSelectionError
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
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent, RecordedMarketEvent
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)


@dataclass(slots=True)
class _Stream:
    """One instrument's independent recording pipeline."""

    instrument: Instrument
    contract: ContractMonth
    symbol: str
    aggregator: RealtimeBarAggregator
    recorder: MarketDataRecorderService


class QuoteRuntime:
    """Records every `Instrument` for the whole quote session.

    小台指 and 大台指 are both registered and aggregated from login onwards; the
    operator's instrument selection (Feature 03) chooses only which of the recorded
    streams `forming_bar`/`query` expose to the chart, and never stops, restarts, or
    re-labels the other one. Contract months follow the selection for the charted
    instrument (an operator's manual month included) and the auto-resolved near month
    for the other.
    """

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
        self._streams: dict[Instrument, _Stream] = {}
        self._by_symbol: dict[str, _Stream] = {}
        self._recording_started_at: Timestamp | None = None
        self._last_cleanup_date: date | None = None
        self._last_stale: bool | None = None
        self._last_event_at: Timestamp | None = None
        self._event_count = 0
        # Mark-to-market feed: at most one `LatestPriceObserved` per second per instrument.
        self._last_price_at: dict[Instrument, Timestamp] = {}
        self._gapped: set[Instrument] = set()
        self._coalesced_price_updates = 0
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
        """The charted instrument's in-progress bar, if any."""
        current = self._selection.current
        if current is None:
            return None
        stream = self._streams.get(current.instrument)
        return None if stream is None else stream.aggregator.forming_bar

    @property
    def recorded_symbols(self) -> tuple[str, ...]:
        return tuple(stream.symbol for stream in self._streams.values())

    @property
    def coalesced_price_updates(self) -> int:
        """How many sub-second `LatestPriceObserved` updates were dropped this run — the
        "被合併／丟棄的高頻更新數" the debug log must be able to report."""
        return self._coalesced_price_updates

    def start(self, user_id: str, password: SecretStr) -> None:
        current = self._selection.current
        if current is None:
            raise RuntimeError("select a contract before quote login")
        # Recording begins here, so every stream is rebuilt against this instant: an
        # aggregator built earlier (an instrument switch made before login) would
        # otherwise carry a cutoff older than the feed it is about to receive.
        self._recording_started_at = self._clock.now()
        self._streams, self._by_symbol = {}, {}
        self._last_price_at, self._gapped, self._coalesced_price_updates = {}, set(), 0
        self._sync_streams()
        self._live.start(user_id, password, self.recorded_symbols)

    def refresh(self) -> None:
        today = self._clock.now().value.date()
        if self._last_cleanup_date != today:
            self._bars.delete_before(rolling_two_month_start(today), ran_at=self._clock.now())
            self._last_cleanup_date = today
        now = self._clock.now()
        for stream in tuple(self._streams.values()):
            stream.recorder.advance(now)
        self._live.refresh()
        stale = self._live.state is not QuoteConnectionState.LOGGED_ON
        if self._streams and stale != self._last_stale:
            log_info(
                _logger,
                "market_data_freshness_changed",
                is_stale=stale,
                quote_state=self._live.state.value,
                event_count=self._event_count,
                coalesced_price_updates=self._coalesced_price_updates,
                recorded_symbols=list(self.recorded_symbols),
                last_event_at=(
                    None if self._last_event_at is None else self._last_event_at.value.isoformat()
                ),
            )
            # One event per recorded stream: staleness is a property of the shared
            # connection, but every downstream consumer keys its own state by
            # (instrument, contract), so publishing for the charted market alone would
            # leave the other recorded market permanently stale-blind.
            for stream in tuple(self._streams.values()):
                self._bus.publish(
                    MarketDataFreshnessChanged(
                        at=now,
                        instrument=stream.instrument,
                        contract=stream.contract,
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
        del event
        # Only the charted market changes here. A stream whose contract is unchanged is
        # left exactly as it is — same aggregator, same forming bar, same cutoff — so
        # switching the view never interrupts or restarts either recording.
        self._sync_streams()
        self._live.select_symbols(self.recorded_symbols)

    def _desired_contracts(self) -> dict[Instrument, ContractMonth]:
        current = self._selection.current
        if current is None:
            return {}
        wanted = {current.instrument: current.contract}
        for instrument in Instrument:
            if instrument in wanted:
                continue
            try:
                wanted[instrument] = self._selection.resolve_near_month(instrument).contract
            except InstrumentSelectionError as exc:
                # A market with no resolvable contract is simply not recorded; it must
                # never take down the charted market's own recording.
                log_warning(
                    _logger,
                    "quote_stream_not_recorded",
                    instrument=instrument.value,
                    reason=str(exc),
                )
        return wanted

    def _sync_streams(self) -> None:
        wanted = self._desired_contracts()
        for instrument in tuple(self._streams):
            if wanted.get(instrument) != self._streams[instrument].contract:
                del self._streams[instrument]
        for instrument, contract in wanted.items():
            if instrument not in self._streams:
                self._streams[instrument] = self._build_stream(instrument, contract)
        self._by_symbol = {stream.symbol: stream for stream in self._streams.values()}

    def _build_stream(self, instrument: Instrument, contract: ContractMonth) -> _Stream:
        entry = self._master.get(instrument, contract)
        if entry is None:
            raise RuntimeError("selected contract is missing from instrument master")
        # A stream created after recording started (a contract change mid-run) has
        # missed the head of whichever boundary it lands in, exactly as a login
        # mid-boundary does, so it takes the current instant as its own cutoff.
        cutoff = self._clock.now()
        started_at = self._recording_started_at
        if started_at is not None and started_at.value > cutoff.value:
            cutoff = started_at
        aggregator = RealtimeBarAggregator(
            instrument,
            contract,
            lambda at: self._calendar.boundary_containing(at, entry),
            cutoff,
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

        def persist(closed: ClosedAggregation) -> None:
            writer.persist(closed)
            # A bar that closed end-to-end proves the feed is whole again for this market.
            self._gapped.discard(instrument)

        def on_trade(recorded: RecordedMarketEvent) -> None:
            self._on_trade(instrument, contract, recorded)

        log_info(
            _logger,
            "quote_stream_configured",
            instrument=instrument.value,
            contract=contract.code,
            symbol=entry.vendor_symbol,
            recording_starts_at=cutoff.value.isoformat(),
        )
        return _Stream(
            instrument,
            contract,
            entry.vendor_symbol,
            aggregator,
            MarketDataRecorderService(
                self._events, MarketEventParser(parse_match_time), aggregator, persist, on_trade
            ),
        )

    def _on_trade(
        self, instrument: Instrument, contract: ContractMonth, recorded: RecordedMarketEvent
    ) -> None:
        if recorded.match_price is None or recorded.matched_at is None:
            return
        last = self._last_price_at.get(instrument)
        if last is not None and (recorded.matched_at.value - last.value).total_seconds() < 1.0:
            self._coalesced_price_updates += 1
            return
        self._last_price_at[instrument] = recorded.matched_at
        if instrument in self._gapped:
            quality = "GAP"
        elif self._live.state is QuoteConnectionState.LOGGED_ON:
            quality = "OK"
        else:
            quality = "STALE"
        self._bus.publish(
            LatestPriceObserved(
                at=self._clock.now(),
                instrument=instrument,
                contract=contract,
                price=recorded.match_price,
                observed_at=recorded.matched_at,
                quality=quality,
            )
        )

    def _on_event(self, event: RawMarketEvent) -> None:
        self._last_event_at = event.received_at
        self._event_count += 1
        stream = self._by_symbol.get(event.symbol)
        if stream is None:
            # A callback for a symbol that is no longer recorded (an in-flight update
            # arriving after a contract change); never attribute it to another market.
            return
        stream.recorder.record(event)

    def _on_gap(self, gap: MarketDataGap) -> None:
        log_warning(
            _logger,
            "market_data_gap_detected",
            symbol=gap.symbol,
            reason=gap.reason,
            started_at=gap.start.value.isoformat(),
        )
        self._events.record_gap(gap)
        stream = self._by_symbol.get(gap.symbol)
        if stream is None:
            return
        stream.aggregator.mark_incomplete()
        self._gapped.add(stream.instrument)
        self._bus.publish(
            MarketDataGapDetected(
                at=self._clock.now(),
                instrument=stream.instrument,
                contract=stream.contract,
                reason=gap.reason,
            )
        )
