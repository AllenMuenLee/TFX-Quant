"""交易日曆 — TAIFEX trading-day/session semantics, built from controlled calendar data.

A calendar *date* is never treated as a trading day just because it isn't a weekend
(holidays must be supplied) or as a non-trading day just because it's a weekend that
happens to be a scheduled trading make-up day — TAIFEX makes both kinds of exception, so
`holidays`/`early_closes` are opaque, externally-supplied data (see
`application.ports.trading_calendar.TradingCalendarRepository` and
`infrastructure.market_data.trading_calendar_repository`), never inferred or guessed
here. Session *times* (day/night start/end) are **not** owned by this module — they
already live per-contract in `InstrumentMasterEntry` (Feature 03); this module only
turns (calendar date + those times) into concrete 60-minute bar boundaries and resolves
a bare exchange time-of-day to the correct trading date, honoring "交易日不可直接等同
曆日" (a trading day is not the same thing as a calendar day) for the night session's
midnight crossing.

Early-close overrides apply only to the day session (a session that does not cross
midnight) — TAIFEX's historical early closes shorten the day session; there is no
precedent (or seed data) for a night-session early close, so `early_closes` is not
consulted for a session whose end date differs from its start date. See
`docs/adr/0006-market-data-and-bar-aggregation.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_TRADING_DAY_SEARCH_OFFSETS = (-1, 0, 1)
"""When resolving a bare time-of-day against a wall-clock receive time, only the
previous/current/next calendar day are ever plausible candidates — ticks are never
delayed by more than one day."""


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    holidays: frozenset[date] = field(default_factory=frozenset)
    """Weekday dates the exchange is closed. Weekends are always non-trading and are not
    enumerated here."""
    early_closes: dict[date, time] = field(default_factory=dict)
    """Date -> overridden day-session end time, for a shortened trading day."""

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def session_end_for(self, d: date, base_end: time) -> time:
        """`base_end` with any early-close override for `d` applied (only ever earlier,
        never later, than the base end)."""
        override = self.early_closes.get(d)
        if override is not None and override < base_end:
            return override
        return base_end

    def bar_boundaries(
        self, d: date, session_start: time, session_end: time
    ) -> list[tuple[Timestamp, Timestamp]]:
        """Ordered (open-label, close) 60-minute boundaries for one session window on
        one trading date. The final boundary is truncated (shorter than 60 minutes)
        whenever the session length isn't a clean hour multiple — normally that only
        happens on an early-close date; both session lengths in the controlled
        instrument master are otherwise exact hour multiples."""
        start_dt, end_dt = self._effective_session_window(d, session_start, session_end)
        boundaries: list[tuple[Timestamp, Timestamp]] = []
        cursor = start_dt
        while cursor < end_dt:
            close = min(cursor + timedelta(hours=1), end_dt)
            boundaries.append((Timestamp(cursor), Timestamp(close)))
            cursor = close
        return boundaries

    def boundary_containing(
        self, at: Timestamp, entry: InstrumentMasterEntry
    ) -> tuple[Timestamp, Timestamp] | None:
        """The single (open-label, close) boundary whose [open, close) window contains
        `at`, searching the trading days around `at`'s calendar date. `None` means `at`
        falls outside every session (a holiday, a weekend, or a genuine no-trade gap
        between sessions)."""
        d = at.value.date()
        for offset in _TRADING_DAY_SEARCH_OFFSETS:
            trading_day = d + timedelta(days=offset)
            if not self.is_trading_day(trading_day):
                continue
            for start, end in _sessions_of(entry):
                for open_ts, close_ts in self.bar_boundaries(trading_day, start, end):
                    if open_ts.value <= at.value < close_ts.value:
                        return open_ts, close_ts
        return None

    def resolve_tick_timestamp(
        self, exchange_time: time, received_at: Timestamp, entry: InstrumentMasterEntry
    ) -> Timestamp | None:
        """Given a bare exchange time-of-day (`MatchTime`, no date) and the wall-clock
        time the push was received, determine which trading date it belongs to — today's
        day session, today's night session, or the tail of *yesterday's* night session
        that spilled past midnight — and return the correctly-dated `Timestamp`. Returns
        `None` when `exchange_time` matches no active session near `received_at` (a
        malformed/rejected tick)."""
        received_date = received_at.value.date()
        best: tuple[float, datetime] | None = None
        for offset in _TRADING_DAY_SEARCH_OFFSETS:
            trading_day = received_date + timedelta(days=offset)
            if not self.is_trading_day(trading_day):
                continue
            for start, end in _sessions_of(entry):
                start_dt, end_dt = self._effective_session_window(trading_day, start, end)
                for candidate_date in {start_dt.date(), end_dt.date()}:
                    candidate = datetime.combine(candidate_date, exchange_time, tzinfo=TAIPEI_TZ)
                    if start_dt <= candidate < end_dt:
                        delta = abs((candidate - received_at.value).total_seconds())
                        if best is None or delta < best[0]:
                            best = (delta, candidate)
        if best is None:
            return None
        return Timestamp(best[1])

    def _effective_session_window(
        self, trading_day: date, start: time, end: time
    ) -> tuple[datetime, datetime]:
        wraps = end <= start
        effective_end = end if wraps else self.session_end_for(trading_day, end)
        start_dt = datetime.combine(trading_day, start, tzinfo=TAIPEI_TZ)
        end_date = trading_day + timedelta(days=1) if wraps else trading_day
        end_dt = datetime.combine(end_date, effective_end, tzinfo=TAIPEI_TZ)
        return start_dt, end_dt


def _sessions_of(entry: InstrumentMasterEntry) -> list[tuple[time, time]]:
    sessions = [(entry.day_session_start, entry.day_session_end)]
    if entry.night_session_start is not None and entry.night_session_end is not None:
        sessions.append((entry.night_session_start, entry.night_session_end))
    return sessions
