"""BarHistoryBackfillService — best-effort `yfinance` gap-fill for the rolling
two-month bar history (Feature 04 extension, see `docs/adr/0007-two-month-bar-history-
persistence.md`'s yfinance extension decision).

`MarketDataBarService` writes a bar promptly whenever its own frequent poll observes it
close (`BarDataSource.POLLED_FROM_YFINANCE`). This service is the second, coarser writer:
on startup, reconnect, contract switch, and daily trading-day rollover, it computes the
exact set of canonical 60-minute bar identities the rolling two-month window expects (per
`TradingCalendar`/`InstrumentMasterEntry` session boundaries, excluding anything not yet
closed), diffs that against what `BarRecordRepository` already has, and asks
`YahooHistoryQueryPort` (the third-party `yfinance` package, isolated behind
`infrastructure.market_data.yfinance_history_adapter` — the same port
`MarketDataBarService` uses) to fill only the missing identities
(`BarDataSource.BACKFILLED_FROM_YFINANCE`) — never a day already fully covered by the
frequent poll.

**Bar-level, not day-level, gap detection.** An earlier revision of this service (vendor
`GetKLine`-based, see ADR 0007's superseded extension section) treated a trading day with
*any* local bar as fully covered. This implementation prompt is explicit that gaps must
be computed "依預期交易時段與 canonical bar identity" — so a day the frequent poll was only
*partially* running for (some hours captured live, others missing) is still correctly
detected and backfilled hour-by-hour.

Runs entirely on its own background thread, never the `EventCoordinator` dispatch
thread — a full run makes several sequential blocking HTTP calls (each already bounded/
retried inside the adapter), which would otherwise stall every other event handler
behind it (see `EventCoordinator`'s single-consumer-thread docstring). Also owns a small
internal timer (mirroring `MarketDataBarService`'s daily-retention-check pattern) so a
trading-day rollover triggers a fresh run even with no broker/instrument-selection event
in between.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BarBackfillCompleted,
    BrokerSessionReady,
    Event,
    InstrumentSwitchCompleted,
)
from tfx_quant.application.market_data.yahoo_bar_resolution import resolve_yahoo_bar
from tfx_quant.application.ports.bar_record_repository import (
    BarRecordRepository,
    BarUpsertOutcome,
    BarUpsertRepositoryError,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.application.ports.yahoo_history_query import (
    YahooBar,
    YahooHistoryQueryError,
    YahooHistoryQueryPort,
)
from tfx_quant.application.ports.yahoo_ticker_mapping import YahooTickerMappingRepository
from tfx_quant.domain.bar_history_backfill import chunk_consecutive_days
from tfx_quant.domain.bar_record import (
    BarConflictAudit,
    BarDataSource,
    BarPeriod,
    BarRecord,
    rolling_two_month_start,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar
from tfx_quant.telemetry import get_logger, log_debug, log_error, log_info, log_warning

_logger = get_logger(__name__)

_DEFAULT_MAX_QUERY_SPAN_DAYS = 10
"""A bounded batch size for one `yfinance` call — not a documented vendor per-call
limit (unlike the superseded `GetKLine` path), just a sane cap so one run never issues
one giant multi-week request or one call per missing hour."""
_DEFAULT_GAP_PADDING_DAYS = 1
"""Calendar days queried on each side of a missing-day chunk, clamped to the rolling
window — the implementation prompt's own "允許為了 API 邊界與連續性檢查在缺口兩側多取
少量資料" allowance. Never changes what gets *written* (still bounded to the window,
instrument, and contract) — only what gets *queried*, so a conflict with existing local
data has a chance to actually surface."""
_DEFAULT_DAILY_CHECK_INTERVAL_SECONDS = 60.0


class EventBus(Protocol):
    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


@dataclass(frozen=True, slots=True)
class _Active:
    instrument: Instrument
    contract: ContractMonth


class BarHistoryBackfillService:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        clock: Clock,
        trading_calendar_repository: TradingCalendarRepository,
        instrument_master: InstrumentMasterRepository,
        bar_record_repository: BarRecordRepository,
        yahoo_ticker_mapping: YahooTickerMappingRepository,
        yahoo_history_query: YahooHistoryQueryPort,
        max_query_span_days: int = _DEFAULT_MAX_QUERY_SPAN_DAYS,
        gap_padding_days: int = _DEFAULT_GAP_PADDING_DAYS,
        daily_check_interval_seconds: float = _DEFAULT_DAILY_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        self._clock = clock
        self._instrument_master = instrument_master
        self._bar_record_repository = bar_record_repository
        self._yahoo_ticker_mapping = yahoo_ticker_mapping
        self._yahoo_history_query = yahoo_history_query
        self._max_query_span_days = max_query_span_days
        self._gap_padding_days = gap_padding_days
        self._daily_check_interval_seconds = daily_check_interval_seconds
        self._calendar = TradingCalendar(
            holidays=trading_calendar_repository.get_holidays(),
            early_closes=trading_calendar_repository.get_early_closes(),
        )

        self._lock = threading.Lock()
        self._active: _Active | None = None
        self._worker: threading.Thread | None = None
        self._rerun_requested = False
        self._last_run_date: date | None = None

        self._running = False
        self._timer: threading.Timer | None = None

        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)
        event_bus.subscribe(InstrumentSwitchCompleted, self._on_instrument_switch)

    # -- Event handlers -----------------------------------------------------------

    def _on_session_ready(self, _event: BrokerSessionReady) -> None:
        self._trigger()

    def _on_instrument_switch(self, event: InstrumentSwitchCompleted) -> None:
        with self._lock:
            self._active = _Active(instrument=event.instrument, contract=event.contract)
        self._trigger()

    # -- Lifecycle (the daily-trading-day-rollover sweep) --------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._schedule_next_tick()
        # Starting the service is itself a startup trigger.  This makes startup
        # deterministic even if InstrumentSwitchCompleted was handled before start(),
        # or an event-bus implementation does not retain pre-start events.
        self._trigger()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_next_tick(self) -> None:
        timer = threading.Timer(self._daily_check_interval_seconds, self._on_timer_fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer_fire(self) -> None:
        self.on_clock_tick()
        with self._lock:
            if self._running:
                self._schedule_next_tick()

    def on_clock_tick(self) -> None:
        """Triggers a fresh run when the wall-clock *date* has changed since the last
        run — an approximation of "每日交易日切換", same simplification
        `MarketDataBarService`'s retention sweep already makes. Public so tests can
        drive it directly without the real background timer."""
        now = self._clock.now()
        with self._lock:
            last = self._last_run_date
        if last is not None and last == now.value.date():
            return
        self._trigger()

    # -- Background-run coordination ------------------------------------------------

    def _trigger(self) -> None:
        with self._lock:
            if self._active is None:
                return
            if self._worker is not None and self._worker.is_alive():
                self._rerun_requested = True
                return
            active = self._active
            worker = threading.Thread(
                target=self._run, args=(active,), name="BarHistoryBackfill", daemon=True
            )
            self._worker = worker
        worker.start()

    def _run(self, active: _Active) -> None:
        try:
            self._backfill_once(active)
        finally:
            with self._lock:
                self._worker = None
                self._last_run_date = self._clock.now().value.date()
                rerun = self._rerun_requested
                self._rerun_requested = False
            if rerun:
                self._trigger()

    # -- Core backfill logic ---------------------------------------------------------

    def _backfill_once(self, active: _Active) -> None:
        entry = self._instrument_master.get(active.instrument, active.contract)
        if entry is None:
            log_warning(
                _logger,
                "bar_backfill_skipped_no_master_entry",
                instrument=active.instrument.value,
                contract=active.contract.code,
            )
            return

        now = self._clock.now()
        window_start = rolling_two_month_start(now.value.date())
        window_end = now.value.date()

        existing = self._bar_record_repository.list_recent(
            active.instrument,
            active.contract,
            BarPeriod.SIXTY_MINUTE,
            since_trading_day=window_start,
        )
        existing_starts = {r.bar.start.value for r in existing}
        missing_starts = sorted(
            (
                open_ts
                for open_ts in self._expected_closed_boundaries(
                    window_start, window_end, entry, now
                )
                if open_ts.value not in existing_starts
            ),
            key=lambda ts: ts.value,
        )
        log_info(
            _logger,
            "bar_backfill_started",
            instrument=active.instrument.value,
            contract=active.contract.code,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            missing_bar_count=len(missing_starts),
        )
        if not missing_starts:
            self._publish_completed(active, requested=0, filled=0, conflicts=0)
            return

        ticker = self._yahoo_ticker_mapping.get(active.instrument, active.contract)
        if ticker is None:
            log_warning(
                _logger,
                "bar_backfill_skipped_no_ticker_mapping",
                instrument=active.instrument.value,
                contract=active.contract.code,
                missing_bar_count=len(missing_starts),
            )
            self._publish_completed(active, requested=len(missing_starts), filled=0, conflicts=0)
            return

        missing_start_set = {ts.value for ts in missing_starts}
        missing_dates = sorted({ts.value.date() for ts in missing_starts})
        chunks = chunk_consecutive_days(missing_dates, self._max_query_span_days)

        filled = 0
        conflicts = 0
        for chunk_start, chunk_end in chunks:
            padded_start = max(window_start, chunk_start - timedelta(days=self._gap_padding_days))
            padded_end = min(window_end, chunk_end + timedelta(days=self._gap_padding_days))
            try:
                yahoo_bars = self._yahoo_history_query.query_1h_bars(
                    yahoo_ticker=ticker, start_date=padded_start, end_date=padded_end
                )
            except YahooHistoryQueryError as exc:
                log_warning(
                    _logger,
                    "bar_backfill_chunk_query_failed",
                    instrument=active.instrument.value,
                    contract=active.contract.code,
                    chunk_start=padded_start.isoformat(),
                    chunk_end=padded_end.isoformat(),
                    error=str(exc),
                    gap_left_unfilled=True,
                )
                continue
            chunk_filled, chunk_conflicts = self._write_yahoo_bars(
                active, entry, yahoo_bars, window_start, window_end, missing_start_set, now
            )
            filled += chunk_filled
            conflicts += chunk_conflicts
            log_info(
                _logger,
                "bar_backfill_chunk_completed",
                instrument=active.instrument.value,
                contract=active.contract.code,
                chunk_start=padded_start.isoformat(),
                chunk_end=padded_end.isoformat(),
                fetched_count=len(yahoo_bars),
                filled_count=chunk_filled,
                conflict_count=chunk_conflicts,
            )

        log_info(
            _logger,
            "bar_backfill_completed",
            instrument=active.instrument.value,
            contract=active.contract.code,
            requested_bar_count=len(missing_starts),
            filled_bar_count=filled,
            conflict_count=conflicts,
        )
        self._publish_completed(
            active, requested=len(missing_starts), filled=filled, conflicts=conflicts
        )

    def _expected_closed_boundaries(
        self,
        window_start: date,
        window_end: date,
        entry: InstrumentMasterEntry,
        now: Timestamp,
    ) -> list[Timestamp]:
        """Every canonical 60-minute bar open-boundary this rolling window expects,
        excluding anything not yet closed as of `now` — never treats a still-forming or
        future bar as "missing"."""
        boundaries: list[Timestamp] = []
        for trading_day in self._calendar.trading_days_between(window_start, window_end):
            sessions = [(entry.day_session_start, entry.day_session_end)]
            if entry.night_session_start is not None and entry.night_session_end is not None:
                sessions.append((entry.night_session_start, entry.night_session_end))
            for start, end in sessions:
                for open_ts, close_ts in self._calendar.bar_boundaries(trading_day, start, end):
                    if close_ts.value <= now.value:
                        boundaries.append(open_ts)
        return boundaries

    def _write_yahoo_bars(
        self,
        active: _Active,
        entry: InstrumentMasterEntry,
        yahoo_bars: Sequence[YahooBar],
        window_start: date,
        window_end: date,
        missing_start_set: set[Any],
        now: Timestamp,
    ) -> tuple[int, int]:
        filled = 0
        conflicts = 0
        for yahoo_bar in yahoo_bars:
            record = self._resolve_one_bar(active, entry, yahoo_bar, window_start, window_end, now)
            if record is None:
                continue
            try:
                outcome = self._bar_record_repository.upsert_closed_bar(record)
            except BarUpsertRepositoryError as exc:
                log_error(
                    _logger,
                    "bar_backfill_row_write_failed",
                    instrument=active.instrument.value,
                    contract=active.contract.code,
                    bar_start=record.bar.start.value.isoformat(),
                    error=str(exc),
                )
                continue
            log_debug(
                _logger,
                "bar_backfill_row_write_result",
                instrument=active.instrument.value,
                contract=active.contract.code,
                bar_start=record.bar.start.value.isoformat(),
                outcome=outcome.value,
            )
            if outcome is BarUpsertOutcome.INSERTED and record.bar.start.value in missing_start_set:
                filled += 1
            elif outcome is BarUpsertOutcome.CONFLICT_REJECTED:
                conflicts += 1
                self._record_conflict(active, record)
        return filled, conflicts

    def _resolve_one_bar(
        self,
        active: _Active,
        entry: InstrumentMasterEntry,
        yahoo_bar: YahooBar,
        window_start: date,
        window_end: date,
        now: Timestamp,
    ) -> BarRecord | None:
        resolved = resolve_yahoo_bar(
            instrument=active.instrument,
            contract=active.contract,
            entry=entry,
            calendar=self._calendar,
            yahoo_bar=yahoo_bar,
            now=now,
        )
        if resolved is None:
            log_debug(
                _logger,
                "bar_backfill_row_dropped_unresolved",
                instrument=active.instrument.value,
                contract=active.contract.code,
                yahoo_at=str(yahoo_bar.at),
            )
            return None
        if resolved.is_forming:
            log_debug(
                _logger,
                "bar_backfill_row_dropped_still_forming",
                instrument=active.instrument.value,
                contract=active.contract.code,
                boundary_open=resolved.bar.start.value.isoformat(),
            )
            return None
        if not (window_start <= resolved.trading_day <= window_end):
            return None  # padding reached outside the two-month window — never written

        return BarRecord(
            bar=resolved.bar,
            period=BarPeriod.SIXTY_MINUTE,
            trading_day=resolved.trading_day,
            session=resolved.session,
            source=BarDataSource.BACKFILLED_FROM_YFINANCE,
            is_gap_recovery=False,
            created_at=now,
            updated_at=now,
        )

    def _record_conflict(self, active: _Active, incoming: BarRecord) -> None:
        existing = self._bar_record_repository.get_one(
            active.instrument, active.contract, BarPeriod.SIXTY_MINUTE, incoming.bar.start
        )
        if existing is None:
            # Vanishingly unlikely (the conflicting row must have existed a moment ago
            # for upsert_closed_bar to have returned CONFLICT_REJECTED at all) but never
            # worth crashing the backfill run over — skip the audit row rather than
            # fabricate an "existing" side.
            log_error(
                _logger,
                "bar_backfill_conflict_audit_skipped_no_existing_row",
                instrument=active.instrument.value,
                contract=active.contract.code,
                bar_start=incoming.bar.start.value.isoformat(),
            )
            return
        try:
            self._bar_record_repository.record_conflict(
                BarConflictAudit(
                    existing=existing, incoming=incoming, detected_at=self._clock.now()
                )
            )
        except BarUpsertRepositoryError as exc:
            log_error(
                _logger,
                "bar_backfill_conflict_audit_write_failed",
                instrument=active.instrument.value,
                contract=active.contract.code,
                bar_start=incoming.bar.start.value.isoformat(),
                error=str(exc),
            )

    def _publish_completed(
        self, active: _Active, *, requested: int, filled: int, conflicts: int
    ) -> None:
        self._event_bus.publish(
            BarBackfillCompleted(
                at=self._clock.now(),
                instrument=active.instrument,
                contract=active.contract,
                requested_bar_count=requested,
                filled_bar_count=filled,
                conflict_count=conflicts,
            )
        )

    # -- Query surface for the UI / readiness screen ---------------------------------

    def is_degraded(self) -> bool:
        """`True` when the currently active contract has no confirmed Yahoo ticker
        mapping, or has at least one unresolved local-vs-yfinance conflict within the
        rolling window — the two backfill-specific conditions the implementation prompt
        requires surfacing as "readiness 顯示 degraded". Always computed fresh from
        current state (never a cached flag that could go stale), same "nothing failed
        yet is reported healthy, not stronger than that" posture as
        `MarketDataBarService.is_persistence_degraded()`."""
        with self._lock:
            active = self._active
        if active is None:
            return False
        if self._yahoo_ticker_mapping.get(active.instrument, active.contract) is None:
            return True
        window_start = rolling_two_month_start(self._clock.now().value.date())
        conflicted = self._bar_record_repository.list_conflicted_trading_days(
            active.instrument,
            active.contract,
            BarPeriod.SIXTY_MINUTE,
            since_trading_day=window_start,
        )
        return len(conflicted) > 0
