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

import queue
import threading
import time as _time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

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
from tfx_quant.application.ports.bar_record_repository import (
    BarRecordRepository,
    BarUpsertRepositoryError,
    RetentionCleanupSummary,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.bar_aggregator import BarAggregator, CandleStreakCounter
from tfx_quant.domain.bar_record import (
    BarDataSource,
    BarPeriod,
    BarRecord,
    continuous_segments,
    rolling_two_month_start,
)
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
_DEFAULT_WRITE_QUEUE_MAXSIZE = 500
_DEFAULT_MAX_WRITE_RETRIES = 5
_WRITE_RETRY_BASE_DELAY_SECONDS = 0.5
_WRITE_RETRY_MAX_DELAY_SECONDS = 10.0
_WRITE_STOP = object()


@dataclass(frozen=True, slots=True)
class RecordedRange:
    """The queryable extent of a contract's persisted two-month bar history — the "最早
    與最晚可用時間" the UI must show, plus whether the full rolling window is actually
    covered yet (never true on/shortly after first activation)."""

    earliest_at: Timestamp
    latest_at: Timestamp
    window_start: date
    """The rolling two-month window's start date as of the query — see
    `domain.bar_record.rolling_two_month_start`."""

    @property
    def covers_full_window(self) -> bool:
        return self.earliest_at.value.date() <= self.window_start


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
        bar_record_repository: BarRecordRepository,
        stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
        clock_interval_seconds: float = _DEFAULT_CLOCK_INTERVAL_SECONDS,
        write_queue_maxsize: int = _DEFAULT_WRITE_QUEUE_MAXSIZE,
        max_write_retries: int = _DEFAULT_MAX_WRITE_RETRIES,
    ) -> None:
        self._event_bus = event_bus
        self._clock = clock
        self._instrument_master = instrument_master
        self._bar_record_repository = bar_record_repository
        self._stale_after_seconds = stale_after_seconds
        self._clock_interval_seconds = clock_interval_seconds
        self._max_write_retries = max_write_retries
        self._calendar = TradingCalendar(
            holidays=trading_calendar_repository.get_holidays(),
            early_closes=trading_calendar_repository.get_early_closes(),
        )

        self._lock = threading.RLock()
        self._active: _ActiveContract | None = None
        self._timer: threading.Timer | None = None
        self._running = False

        self._write_queue: queue.Queue[object] = queue.Queue(maxsize=write_queue_maxsize)
        self._write_thread: threading.Thread | None = None
        self._degraded_lock = threading.Lock()
        self._is_degraded = False
        self._last_retention_check_date: date | None = None
        self._last_retention_summary: RetentionCleanupSummary | None = None

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
            active = _ActiveContract(
                instrument=instrument,
                contract=contract,
                entry=entry,
                vendor_symbol=entry.vendor_symbol,
                aggregator=BarAggregator(
                    instrument=instrument, contract=contract, entry=entry, calendar=self._calendar
                ),
                streak=CandleStreakCounter(),
            )
            self._load_warm_up(active, self._clock.now())
            self._active = active

    # -- Lifecycle (the periodic no-tick-required sweep) -----------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._schedule_next_tick()
            self._write_thread = threading.Thread(
                target=self._run_writer, name="BarRecordWriter", daemon=True
            )
            self._write_thread.start()
        self._run_retention_cleanup(self._clock.now())

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            write_thread = self._write_thread
            self._write_thread = None
        if write_thread is not None:
            self._write_queue.put(_WRITE_STOP)
            write_thread.join(timeout=5.0)

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
        self._maybe_run_daily_retention_cleanup(now)
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
            # Reconnect: re-sync the in-memory streak/recent-bars view from whatever
            # this process (or an earlier one) has actually persisted, same as clear()
            # does on a fresh switch — see the implementation prompt's "啟動、重連或
            # 切換契約時...載入" requirement.
            self._load_warm_up(active, self._clock.now())
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

    def _load_warm_up(self, active: _ActiveContract, now: Timestamp) -> None:
        """Loads this contract's rolling two-month persisted history and replays it
        into a fresh streak counter / recent-bars view — the "重啟後可還原最近兩個月 K
        棒與紅／黑／十字狀態" acceptance criterion. Safe to call repeatedly (clear() and
        every reconnect) since it always rebuilds from the repository's current state
        rather than incrementally patching in-memory state."""
        window_start = rolling_two_month_start(now.value.date())
        records = self._bar_record_repository.list_recent(
            active.instrument,
            active.contract,
            BarPeriod.SIXTY_MINUTE,
            since_trading_day=window_start,
        )
        streak = CandleStreakCounter()
        for record in records:
            streak.on_bar_closed(record.bar)
        active.streak = streak
        tail = records[-_RECENT_BARS_LIMIT:]
        active.recent_closed = deque((r.bar for r in tail), maxlen=_RECENT_BARS_LIMIT)

    def _handle_closed_bars(
        self, active: _ActiveContract, closed_bars: list[Bar], now: Timestamp
    ) -> None:
        for bar in closed_bars:
            active.recent_closed.append(bar)
            active.streak.on_bar_closed(bar)
            is_gap_recovery = active.has_gap
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
            self._enqueue_for_persistence(active, bar, is_gap_recovery=is_gap_recovery, now=now)

    def _enqueue_for_persistence(
        self, active: _ActiveContract, bar: Bar, *, is_gap_recovery: bool, now: Timestamp
    ) -> None:
        context = self._calendar.session_context_for(bar.start, active.entry)
        if context is None:
            # Never expected — bar.start is always one of the calendar's own boundary
            # outputs — but a persistence-layer surprise must never crash tick
            # processing; flag degraded and drop this one write.
            self._set_degraded(True)
            return
        trading_day, session = context
        record = BarRecord(
            bar=bar,
            period=BarPeriod.SIXTY_MINUTE,
            trading_day=trading_day,
            session=session,
            source=BarDataSource.AGGREGATED_FROM_YUANTA_REALTIME,
            is_gap_recovery=is_gap_recovery,
            created_at=now,
            updated_at=now,
        )
        try:
            self._write_queue.put_nowait(record)
        except queue.Full:
            self._set_degraded(True)

    def _run_writer(self) -> None:
        """Runs on its own daemon thread — never the `EventCoordinator` consumer
        thread — so a slow or failing database never blocks tick processing."""
        while True:
            item = self._write_queue.get()
            if item is _WRITE_STOP:
                return
            assert isinstance(item, BarRecord)
            self._write_with_bounded_retry(item)

    def _write_with_bounded_retry(self, record: BarRecord) -> None:
        attempt = 0
        while True:
            try:
                self._bar_record_repository.upsert_closed_bar(record)
                self._set_degraded(False)
                return
            except BarUpsertRepositoryError:
                attempt += 1
                if attempt >= self._max_write_retries:
                    self._set_degraded(True)
                    return
                delay = min(
                    _WRITE_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                    _WRITE_RETRY_MAX_DELAY_SECONDS,
                )
                _time.sleep(delay)

    def _set_degraded(self, degraded: bool) -> None:
        with self._degraded_lock:
            changed = degraded != self._is_degraded
            self._is_degraded = degraded
        if changed:
            self._publish(BarPersistenceHealthChanged(at=Timestamp.now(), is_degraded=degraded))

    def _run_retention_cleanup(self, now: Timestamp) -> None:
        cutoff = rolling_two_month_start(now.value.date())
        try:
            summary = self._bar_record_repository.delete_before(cutoff, ran_at=now)
        except BarUpsertRepositoryError:
            return  # best-effort — a failed sweep is not itself a data-loss risk
        with self._lock:
            self._last_retention_summary = summary
            self._last_retention_check_date = now.value.date()
        self._publish(
            BarRetentionCleanupCompleted(
                at=now,
                cutoff_trading_day=summary.cutoff_trading_day,
                deleted_count=summary.deleted_count,
            )
        )

    def _maybe_run_daily_retention_cleanup(self, now: Timestamp) -> None:
        with self._lock:
            last_checked = self._last_retention_check_date
        if last_checked is not None and last_checked == now.value.date():
            return
        self._run_retention_cleanup(now)

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

    def candle_streak(
        self, instrument: Instrument, contract: ContractMonth
    ) -> tuple[CandleColor | None, int]:
        """紅／黑／十字K streak — `(None, 0)` when inactive or nothing has closed since
        the last DOJI reset. Restored from persisted history by `clear()`/reconnect (see
        `_load_warm_up`), not just accumulated in-memory since process start — the
        "重啟後可還原...紅／黑／十字狀態" acceptance criterion."""
        with self._lock:
            active = self._matching_active(instrument, contract)
            if active is None:
                return None, 0
            return active.streak.color, active.streak.length

    # -- Persisted two-month history query surface -----------------------------------

    def continuous_warm_up_bars(
        self, instrument: Instrument, contract: ContractMonth
    ) -> Sequence[Bar]:
        """The most recent gap-free run of persisted bars — the only slice of stored
        history "通過交易日曆與連續性檢查" and therefore safe for a future strategy
        engine to warm up signal state from. Any single missing boundary anywhere in
        the persisted history breaks the chain; only the tail run reaching the most
        recently stored bar is ever returned."""
        window_start = rolling_two_month_start(self._clock.now().value.date())
        records = self._bar_record_repository.list_recent(
            instrument, contract, BarPeriod.SIXTY_MINUTE, since_trading_day=window_start
        )
        segments = continuous_segments(records)
        return segments[-1].bars if segments else ()

    def recorded_range(
        self, instrument: Instrument, contract: ContractMonth
    ) -> RecordedRange | None:
        """`None` means nothing has ever been persisted for this (instrument, contract)
        — the normal state on first activation, before the first bar has closed."""
        earliest = self._bar_record_repository.earliest_recorded_at(
            instrument, contract, BarPeriod.SIXTY_MINUTE
        )
        if earliest is None:
            return None
        latest = self._bar_record_repository.latest_recorded_at(
            instrument, contract, BarPeriod.SIXTY_MINUTE
        )
        assert latest is not None  # a repository with an earliest row always has a latest
        window_start = rolling_two_month_start(self._clock.now().value.date())
        return RecordedRange(earliest_at=earliest, latest_at=latest, window_start=window_start)

    def query_history(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        *,
        start_date: date,
        end_date: date,
    ) -> Sequence[BarRecord]:
        """The UI's browse-by-date-range query — every persisted record (including
        source/completeness metadata) with a trading day in `[start_date, end_date]`."""
        return self._bar_record_repository.query_range(
            instrument, contract, BarPeriod.SIXTY_MINUTE, start_date=start_date, end_date=end_date
        )

    def is_persistence_degraded(self) -> bool:
        """Surfaced on the startup diagnostics readiness screen — see
        `desktop.composition.compute_readiness`. `False` until a write has actually
        failed after bounded retry or the write queue has filled up; nothing failing
        yet is not the same as everything being healthy, but this codebase has no
        stronger guarantee to offer before a first failure is observed."""
        with self._degraded_lock:
            return self._is_degraded

    def last_retention_summary(self) -> RetentionCleanupSummary | None:
        with self._lock:
            return self._last_retention_summary

    def _matching_active(
        self, instrument: Instrument, contract: ContractMonth
    ) -> _ActiveContract | None:
        active = self._active
        if active is None or active.instrument != instrument or active.contract != contract:
            return None
        return active
