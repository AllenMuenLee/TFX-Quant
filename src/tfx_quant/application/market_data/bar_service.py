"""MarketDataBarService — the real `BarSignalStateStore`, and the tick -> bar pipeline.

Closes both forward gaps ADR 0005 left open: `NullBarSignalStateStore` was always a
placeholder ("real bar/signal state is Feature 04/05's job"), and nothing has ever
consumed the vendor's price pushes despite `QuoteGatewayPort.subscribe()` registering
for them since Feature 03.

`clear(instrument, contract)` (the `BarSignalStateStore` method) is the primary hook for
learning which (instrument, contract) is currently active — `InstrumentSelectionService.
switch_to()` calls it synchronously, with the *new* selection, right before publishing
`InstrumentSwitchCompleted` (see `instrument_selection_service.py`). Since this service
already holds an `InstrumentMasterRepository`, `clear()` alone is enough to resolve the
new contract's session times and vendor symbol — no separate `InstrumentSwitchCompleted`
subscription is needed, and using the synchronous contract call instead of the
asynchronous event avoids a race between "state cleared" and "first tick arrives".

Every `_ActiveContract` field is protected by `_lock` — ticks arrive on the
`EventCoordinator` consumer thread, the staleness/no-trade-interval sweep fires on this
service's own internal timer thread, and UI query methods are called from the wx thread.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BarClosed,
    BrokerSessionReady,
    Event,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
    MarketDataTickReceived,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_aggregator import BarAggregator, CandleStreakCounter
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.tick import Tick
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar

_DEFAULT_STALE_AFTER_SECONDS = 10.0
_DEFAULT_CLOCK_INTERVAL_SECONDS = 1.0
_RECENT_BARS_LIMIT = 20


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as
    `session_orchestrator.EventPublisher`, extended with `subscribe` since this service
    needs to react to `MarketDataTickReceived`/`BrokerSessionReady`, not just publish."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


@dataclass
class _ActiveContract:
    instrument: Instrument
    contract: ContractMonth
    entry: InstrumentMasterEntry
    vendor_symbol: str
    aggregator: BarAggregator
    streak: CandleStreakCounter
    recent_closed: deque[Bar] = field(default_factory=lambda: deque(maxlen=_RECENT_BARS_LIMIT))
    last_tick_at: Timestamp | None = None
    is_stale: bool = True
    has_gap: bool = False


class MarketDataBarService:
    """Implements `application.ports.bar_signal_state.BarSignalStateStore`."""

    def __init__(
        self,
        *,
        event_bus: EventBus,
        clock: Clock,
        trading_calendar_repository: TradingCalendarRepository,
        instrument_master: InstrumentMasterRepository,
        stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
        clock_interval_seconds: float = _DEFAULT_CLOCK_INTERVAL_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        self._clock = clock
        self._instrument_master = instrument_master
        self._stale_after_seconds = stale_after_seconds
        self._clock_interval_seconds = clock_interval_seconds
        self._calendar = TradingCalendar(
            holidays=trading_calendar_repository.get_holidays(),
            early_closes=trading_calendar_repository.get_early_closes(),
        )

        self._lock = threading.RLock()
        self._active: _ActiveContract | None = None
        self._timer: threading.Timer | None = None
        self._running = False

        event_bus.subscribe(MarketDataTickReceived, self._on_tick_received)
        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)

    # -- BarSignalStateStore --------------------------------------------------------

    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        with self._lock:
            entry = self._instrument_master.get(instrument, contract)
            if entry is None:
                # Nothing to track without controlled session-time data — leave
                # inactive rather than guessing session boundaries.
                self._active = None
                return
            self._active = _ActiveContract(
                instrument=instrument,
                contract=contract,
                entry=entry,
                vendor_symbol=entry.vendor_symbol,
                aggregator=BarAggregator(
                    instrument=instrument, contract=contract, entry=entry, calendar=self._calendar
                ),
                streak=CandleStreakCounter(),
            )

    # -- Lifecycle (the periodic no-tick-required sweep) -----------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._schedule_next_tick()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_next_tick(self) -> None:
        timer = threading.Timer(self._clock_interval_seconds, self._on_timer_fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer_fire(self) -> None:
        self.on_clock_tick()
        with self._lock:
            if self._running:
                self._schedule_next_tick()

    def on_clock_tick(self) -> None:
        """Advances the active aggregator with no tick required, so a forming bar with
        trades still closes promptly and staleness is detected even during a lull.
        Public so tests can drive it directly with a fake `Clock`, without starting the
        real background timer."""
        now = self._clock.now()
        with self._lock:
            active = self._active
            if active is None:
                return
            closed_bars = active.aggregator.on_clock(now)
            self._handle_closed_bars(active, closed_bars, now)
            self._update_staleness(active, now)

    # -- Event handlers ---------------------------------------------------------------

    def _on_tick_received(self, event: MarketDataTickReceived) -> None:
        with self._lock:
            active = self._active
            if active is None or event.vendor_symbol != active.vendor_symbol:
                return  # not currently tracking this symbol — the "驗證商品" criterion
            now = self._clock.now()
            resolved_at = self._calendar.resolve_tick_timestamp(
                event.exchange_time, now, active.entry
            )
            if resolved_at is None:
                return  # exchange time matches no active session near now — reject
            try:
                tick = Tick(
                    instrument=active.instrument,
                    contract=active.contract,
                    at=resolved_at,
                    price=Price(event.price),
                    size=event.size,
                    cumulative_volume=event.cumulative_volume,
                )
            except DomainError:
                return  # malformed push — reject rather than raise into the dispatch loop
            closed_bars = active.aggregator.on_tick(tick)
            active.last_tick_at = now
            self._update_staleness(active, now)
            self._handle_closed_bars(active, closed_bars, now)

    def _on_session_ready(self, _event: BrokerSessionReady) -> None:
        with self._lock:
            active = self._active
            if active is None:
                return
            if not active.has_gap:
                active.has_gap = True
                self._publish(
                    MarketDataGapDetected(
                        at=Timestamp.now(),
                        instrument=active.instrument,
                        contract=active.contract,
                        reason=(
                            "連線建立後尚無足夠歷史資料可重建目前 K 棒"
                            "（本系統無已確認之歷史/tick 補洞機制），"
                            "需等待下一根 K 棒完整收盤後才會解除"
                        ),
                    )
                )

    # -- Internal helpers ---------------------------------------------------------

    def _handle_closed_bars(
        self, active: _ActiveContract, closed_bars: list[Bar], now: Timestamp
    ) -> None:
        for bar in closed_bars:
            active.recent_closed.append(bar)
            active.streak.on_bar_closed(bar)
            self._publish(
                BarClosed(at=now, instrument=active.instrument, contract=active.contract, bar=bar)
            )
            if active.has_gap:
                active.has_gap = False
                self._publish(
                    MarketDataGapCleared(
                        at=now, instrument=active.instrument, contract=active.contract
                    )
                )

    def _update_staleness(self, active: _ActiveContract, now: Timestamp) -> None:
        if active.last_tick_at is None:
            is_stale_now = True
        else:
            age = (now.value - active.last_tick_at.value).total_seconds()
            is_stale_now = age > self._stale_after_seconds
        if is_stale_now != active.is_stale:
            active.is_stale = is_stale_now
            self._publish(
                MarketDataFreshnessChanged(
                    at=now,
                    instrument=active.instrument,
                    contract=active.contract,
                    is_stale=is_stale_now,
                )
            )

    def _publish(self, event: Event) -> None:
        self._event_bus.publish(event)

    # -- Query surface for the UI (Feature 04 acceptance criteria) ------------------

    def forming_bar(self, instrument: Instrument, contract: ContractMonth) -> Bar | None:
        with self._lock:
            active = self._matching_active(instrument, contract)
            return active.aggregator.forming_bar_snapshot() if active is not None else None

    def recent_closed_bars(
        self, instrument: Instrument, contract: ContractMonth, limit: int = _RECENT_BARS_LIMIT
    ) -> Sequence[Bar]:
        with self._lock:
            active = self._matching_active(instrument, contract)
            if active is None:
                return ()
            return tuple(list(active.recent_closed)[-limit:])

    def is_stale(self, instrument: Instrument, contract: ContractMonth) -> bool:
        with self._lock:
            active = self._matching_active(instrument, contract)
            return True if active is None else active.is_stale

    def has_gap(self, instrument: Instrument, contract: ContractMonth) -> bool:
        with self._lock:
            active = self._matching_active(instrument, contract)
            return False if active is None else active.has_gap

    def last_update_at(
        self, instrument: Instrument, contract: ContractMonth
    ) -> Timestamp | None:
        with self._lock:
            active = self._matching_active(instrument, contract)
            return active.last_tick_at if active is not None else None

    def _matching_active(
        self, instrument: Instrument, contract: ContractMonth
    ) -> _ActiveContract | None:
        active = self._active
        if active is None or active.instrument != instrument or active.contract != contract:
            return None
        return active
