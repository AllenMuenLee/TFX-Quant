"""TradingCalendarRepository — the controlled 交易日曆 (holiday/early-close) lookup port.

Mirrors `InstrumentMasterRepository`'s "controlled data, not a formula" pattern (ADR
0005): no vendor API documents an exchange holiday calendar, so this is backed by a
version-controlled JSON file (`infrastructure.market_data.trading_calendar_repository`),
never computed. Session start/end times stay on `InstrumentMasterEntry` (Feature 03) —
this port only supplies the two things that aren't already there: which dates are
closed, and which dates have a shortened (early-close) day session.
"""

from __future__ import annotations

from datetime import date, time
from typing import Protocol


class TradingCalendarRepository(Protocol):
    def get_holidays(self) -> frozenset[date]: ...

    def get_early_closes(self) -> dict[date, time]:
        """Date -> overridden day-session end time, for every configured early-close
        date. Bulk (not a per-date lookup) since `domain.trading_calendar.TradingCalendar`
        is built once from this data, not re-queried per tick."""
        ...
