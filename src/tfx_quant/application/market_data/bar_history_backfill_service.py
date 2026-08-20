"""BarHistoryBackfillService — best-effort vendor `GetKLine` gap-fill for the rolling
two-month bar history (Feature 04 extension, see `docs/adr/0007-two-month-bar-history-
persistence.md`'s extension decision).

`MarketDataBarService` only ever writes a bar this process itself aggregated from a live
tick. This service is the second, distinct writer the extension prompt added: on login
and on every contract switch, it finds which trading days in the rolling two-month window
have *no* locally-recorded bar at all, and asks `HistoricalPriceQueryPort` (the vendor's
official `GetKLine` history query) to fill them. It never touches a day that already has
local data — `BarRecordRepository.upsert_closed_bar`'s own dedup/conflict semantics are
the second line of defense, but this service's own day-level gap check is the first: a
day with a local bar (whatever its source) is simply skipped.

Runs entirely on its own background thread, never on the `EventCoordinator` dispatch
thread — a full backfill run makes several sequential blocking vendor calls (each paced
at the vendor's documented ≤1/sec rate for `GetKLine`), which would otherwise stall every
other event handler behind it (see `EventCoordinator`'s single-consumer-thread docstring).
"""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BarBackfillCompleted,
    BrokerSessionReady,
    Event,
    InstrumentSwitchCompleted,
)
from tfx_quant.application.ports.bar_record_repository import (
    BarRecordRepository,
    BarUpsertRepositoryError,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.historical_price_query import (
    HistoricalPriceQueryError,
    HistoricalPriceQueryPort,
    VendorKLineBar,
)
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_history_backfill import chunk_consecutive_days
from tfx_quant.domain.bar_record import (
    BarDataSource,
    BarPeriod,
    BarRecord,
    rolling_two_month_start,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar
from tfx_quant.telemetry import get_logger, log_debug, log_error, log_info, log_warning

_logger = get_logger(__name__)

_MAX_QUERY_SPAN_DAYS = 5
"""60分k 単次查詢限制 per the GetKLine docs' own K線種類查詢限制 table."""
_KLINE_MIN_INTERVAL_SECONDS = 1.0
"""GetKLine's own documented rate cap: ≤1 call/sec, tracked separately from the general
quote/account 3/sec cap."""


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
        historical_price_query: HistoricalPriceQueryPort,
        max_query_span_days: int = _MAX_QUERY_SPAN_DAYS,
        min_query_interval_seconds: float = _KLINE_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._event_bus = event_bus
        self._clock = clock
        self._instrument_master = instrument_master
        self._bar_record_repository = bar_record_repository
        self._historical_price_query = historical_price_query
        self._max_query_span_days = max_query_span_days
        self._min_query_interval_seconds = min_query_interval_seconds
        self._calendar = TradingCalendar(
            holidays=trading_calendar_repository.get_holidays(),
            early_closes=trading_calendar_repository.get_early_closes(),
        )

        self._lock = threading.Lock()
        self._account: TradingAccount | None = None
        self._active: _Active | None = None
        self._worker: threading.Thread | None = None
        self._rerun_requested = False

        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)
        event_bus.subscribe(InstrumentSwitchCompleted, self._on_instrument_switch)

    # -- Event handlers ---------------------------------------------------------------

    def _on_session_ready(self, event: BrokerSessionReady) -> None:
        with self._lock:
            self._account = event.account
        self._trigger()

    def _on_instrument_switch(self, event: InstrumentSwitchCompleted) -> None:
        with self._lock:
            self._active = _Active(instrument=event.instrument, contract=event.contract)
        self._trigger()

    # -- Background-run coordination ---------------------------------------------------

    def _trigger(self) -> None:
        with self._lock:
            if self._account is None or self._active is None:
                return
            if self._worker is not None and self._worker.is_alive():
                self._rerun_requested = True
                return
            account = self._account
            active = self._active
            worker = threading.Thread(
                target=self._run, args=(account, active), name="BarHistoryBackfill", daemon=True
            )
            self._worker = worker
        worker.start()

    def _run(self, account: TradingAccount, active: _Active) -> None:
        try:
            self._backfill_once(account, active)
        finally:
            rerun = False
            with self._lock:
                self._worker = None
                if self._rerun_requested:
                    self._rerun_requested = False
                    rerun = True
            if rerun:
                self._trigger()

    # -- Core backfill logic ------------------------------------------------------------

    def _backfill_once(self, account: TradingAccount, active: _Active) -> None:
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
        covered_days = {record.trading_day for record in existing}
        trading_days = self._calendar.trading_days_between(window_start, window_end)
        missing_days = [d for d in trading_days if d not in covered_days]
        log_info(
            _logger,
            "bar_backfill_started",
            instrument=active.instrument.value,
            contract=active.contract.code,
            vendor_symbol=entry.vendor_symbol,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            local_covered_day_count=len(covered_days),
            missing_day_count=len(missing_days),
        )

        filled_days: set[date] = set()
        chunks = chunk_consecutive_days(missing_days, self._max_query_span_days)
        for chunk_start, chunk_end in chunks:
            try:
                vendor_bars = self._historical_price_query.query_60m_kline(
                    account=account,
                    vendor_symbol=entry.vendor_symbol,
                    start_date=chunk_start,
                    end_date=chunk_end,
                )
            except HistoricalPriceQueryError as exc:
                # Leave this chunk as a gap — retried, if at all, on the next trigger
                # (a fresh reconnect or contract switch), never looped synchronously.
                log_warning(
                    _logger,
                    "bar_backfill_chunk_query_failed",
                    instrument=active.instrument.value,
                    contract=active.contract.code,
                    chunk_start=chunk_start.isoformat(),
                    chunk_end=chunk_end.isoformat(),
                    error=str(exc),
                    gap_left_unfilled=True,
                )
                _time.sleep(self._min_query_interval_seconds)
                continue
            written = self._write_vendor_bars(active, entry, vendor_bars)
            log_info(
                _logger,
                "bar_backfill_chunk_completed",
                instrument=active.instrument.value,
                contract=active.contract.code,
                chunk_start=chunk_start.isoformat(),
                chunk_end=chunk_end.isoformat(),
                source="vendor_kline",
                fetched_count=len(vendor_bars),
                written_day_count=len(written),
            )
            filled_days |= written
            _time.sleep(self._min_query_interval_seconds)

        log_info(
            _logger,
            "bar_backfill_completed",
            instrument=active.instrument.value,
            contract=active.contract.code,
            requested_day_count=len(missing_days),
            filled_day_count=len(filled_days),
            warm_up_allowed=len(filled_days) > 0 or len(missing_days) == 0,
        )
        self._event_bus.publish(
            BarBackfillCompleted(
                at=self._clock.now(),
                instrument=active.instrument,
                contract=active.contract,
                requested_day_count=len(missing_days),
                filled_day_count=len(filled_days),
            )
        )

    def _write_vendor_bars(
        self,
        active: _Active,
        entry: InstrumentMasterEntry,
        vendor_bars: Sequence[VendorKLineBar],
    ) -> set[date]:
        filled_days: set[date] = set()
        for vendor_bar in vendor_bars:
            written_day = self._write_one_vendor_bar(active, entry, vendor_bar)
            if written_day is not None:
                filled_days.add(written_day)
        return filled_days

    def _write_one_vendor_bar(
        self, active: _Active, entry: InstrumentMasterEntry, vendor_bar: VendorKLineBar
    ) -> date | None:
        try:
            open_ts = Timestamp(vendor_bar.at)
        except DomainError as exc:
            log_debug(
                _logger,
                "bar_backfill_row_dropped_malformed_timestamp",
                instrument=active.instrument.value,
                contract=active.contract.code,
                vendor_at=str(vendor_bar.at),
                error=str(exc),
            )
            return None  # malformed vendor timestamp — never fabricated, left as a gap
        boundary = self._calendar.boundary_for_open(open_ts, entry)
        if boundary is None:
            # Off-grid vendor timestamp (or the open-label assumption is wrong for this
            # push) — never snapped or guessed at, left as an unresolved gap.
            log_debug(
                _logger,
                "bar_backfill_row_dropped_off_grid",
                instrument=active.instrument.value,
                contract=active.contract.code,
                vendor_open_at=open_ts.value.isoformat(),
            )
            return None
        boundary_open, boundary_close = boundary
        context = self._calendar.session_context_for(boundary_open, entry)
        if context is None:
            log_debug(
                _logger,
                "bar_backfill_row_dropped_no_session_context",
                instrument=active.instrument.value,
                contract=active.contract.code,
                boundary_open=boundary_open.value.isoformat(),
            )
            return None
        trading_day, session = context

        try:
            bar = Bar(
                instrument=active.instrument,
                contract=active.contract,
                open=Price(vendor_bar.open),
                high=Price(vendor_bar.high),
                low=Price(vendor_bar.low),
                close=Price(vendor_bar.close),
                volume=vendor_bar.volume,
                start=boundary_open,
                end=boundary_close,
            )
        except DomainError as exc:
            log_debug(
                _logger,
                "bar_backfill_row_dropped_invalid_ohlcv",
                instrument=active.instrument.value,
                contract=active.contract.code,
                boundary_open=boundary_open.value.isoformat(),
                error=str(exc),
            )
            return None

        now = self._clock.now()
        record = BarRecord(
            bar=bar,
            period=BarPeriod.SIXTY_MINUTE,
            trading_day=trading_day,
            session=session,
            source=BarDataSource.BACKFILLED_FROM_YUANTA_KLINE,
            is_gap_recovery=False,
            created_at=now,
            updated_at=now,
        )
        try:
            outcome = self._bar_record_repository.upsert_closed_bar(record)
        except BarUpsertRepositoryError as exc:
            log_error(
                _logger,
                "bar_backfill_row_write_failed",
                instrument=active.instrument.value,
                contract=active.contract.code,
                trading_day=trading_day.isoformat(),
                error=str(exc),
            )
            return None
        log_debug(
            _logger,
            "bar_backfill_row_write_result",
            instrument=active.instrument.value,
            contract=active.contract.code,
            trading_day=trading_day.isoformat(),
            outcome=outcome.value,
        )
        return trading_day
