from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum

from tfx_quant.application.ports.bar_record_repository import BarRecordRepository
from tfx_quant.application.ports.market_event_repository import MarketEventRepository
from tfx_quant.domain.bar_record import BarPeriod, BarRecord, rolling_two_month_start
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.market_data import MarketDataGap
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


class HistoryReadiness(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class HistoryCoverage:
    requested_start: Timestamp
    requested_end: Timestamp
    available_start: Timestamp | None
    available_end: Timestamp | None
    complete_closed_bar_count: int
    gaps: tuple[MarketDataGap, ...]
    recorder_first_available_at: Timestamp | None
    last_received_at: Timestamp | None
    stale: bool
    readiness: HistoryReadiness


@dataclass(frozen=True, slots=True)
class LocalBarHistory:
    bars: tuple[BarRecord, ...]
    coverage: HistoryCoverage


class LocalBarHistoryService:
    def __init__(
        self,
        bars: BarRecordRepository,
        events: MarketEventRepository,
        *,
        stale_after_seconds: float = 90.0,
    ) -> None:
        self._bars = bars
        self._events = events
        self._stale_after_seconds = stale_after_seconds

    def query_two_months(
        self, instrument: Instrument, contract: ContractMonth, symbol: str, now: Timestamp
    ) -> LocalBarHistory:
        start_date = rolling_two_month_start(now.value.date())
        requested_start = Timestamp(datetime.combine(start_date, time.min, tzinfo=TAIPEI_TZ))
        records = tuple(
            self._bars.query_range(
                instrument,
                contract,
                BarPeriod.SIXTY_MINUTE,
                start_date=start_date,
                end_date=now.value.date(),
            )
        )
        events = tuple(self._events.list_events(symbol))
        gaps_list = list(self._events.list_gaps(symbol))
        first_event = events[0].raw.received_at if events else None
        last_event = events[-1].raw.received_at if events else None
        if first_event is not None and first_event.value > requested_start.value:
            gaps_list.append(
                MarketDataGap(symbol, requested_start, first_event, "before recorder activation")
            )
        for previous, current in zip(records, records[1:], strict=False):
            if current.bar.start.value > previous.bar.end.value:
                gaps_list.append(
                    MarketDataGap(
                        symbol, previous.bar.end, current.bar.start, "missing closed-bar interval"
                    )
                )
        gaps = tuple(sorted(gaps_list, key=lambda gap: gap.start.value))
        stale = (
            last_event is None
            or (now.value - last_event.value).total_seconds() > self._stale_after_seconds
        )
        complete = tuple(r for r in records if r.is_complete)
        available_start = records[0].bar.start if records else None
        available_end = records[-1].bar.end if records else None
        full_window = available_start is not None and available_start.value <= requested_start.value
        readiness = (
            HistoryReadiness.READY
            if full_window and not gaps and not stale and len(complete) == len(records)
            else HistoryReadiness.BLOCKED
            if not complete
            else HistoryReadiness.DEGRADED
        )
        return LocalBarHistory(
            records,
            HistoryCoverage(
                requested_start,
                now,
                available_start,
                available_end,
                len(complete),
                gaps,
                first_event,
                last_event,
                stale,
                readiness,
            ),
        )
