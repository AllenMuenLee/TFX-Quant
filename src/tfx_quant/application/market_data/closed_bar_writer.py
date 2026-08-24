"""Transactional hand-off from live aggregation to local closed-bar history."""

from __future__ import annotations

from collections.abc import Callable

from tfx_quant.application.market_data.realtime_bar_aggregator import ClosedAggregation
from tfx_quant.application.ports.bar_record_repository import (
    BarRecordRepository,
    BarUpsertOutcome,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord


class LocalClosedBarWriter:
    def __init__(
        self,
        repository: BarRecordRepository,
        clock: Clock,
        publish_after_commit: Callable[[BarRecord], None],
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._publish = publish_after_commit

    def persist(self, closed: ClosedAggregation) -> BarUpsertOutcome:
        now = self._clock.now()
        record = BarRecord(
            bar=closed.bar,
            period=BarPeriod.SIXTY_MINUTE,
            trading_day=closed.trading_day,
            session=closed.session,
            source=BarDataSource.LOCAL_YUANTA_REALTIME,
            is_gap_recovery=False,
            created_at=now,
            updated_at=now,
            source_first_sequence=closed.first_sequence,
            source_last_sequence=closed.last_sequence,
            is_complete=True,
        )
        outcome = self._repository.upsert_closed_bar(record)
        if outcome is BarUpsertOutcome.INSERTED:
            # Repository commit completed before this UI/strategy-visible publication.
            self._publish(record)
        return outcome
