"""YahooHistoryQueryPort — the controlled seam for the third-party `yfinance` package
(Feature 04 extension: see `docs/adr/0007-two-month-bar-history-persistence.md`'s
yfinance extension decision).

Only `infrastructure.market_data` may ever import `yfinance`/`pandas` types — this port
exists specifically so `application.market_data.bar_history_backfill_service.
BarHistoryBackfillService` can ask for a range of already-closed 1-hour bars without
knowing anything about `yfinance`'s `DataFrame`/`Ticker` shape. Every field below is
already a plain typed primitive, normalized by the adapter (column names, timezone,
decimal conversion, dedup, ordering, finite-value checks) before this codebase ever sees
it — see `infrastructure.market_data.yfinance_history_adapter`'s module docstring for
exactly what "normalized" means.

**Honesty caveat: `yfinance` is an unofficial, third-party data source — never Yuanta's
or TAIFEX's own official record.** Every bar this port returns is written with
`domain.bar_record.BarDataSource.BACKFILLED_FROM_YFINANCE`, never conflated with a bar
this process aggregated itself from a real-time Yuanta push. Whether Yahoo Finance
publishes any usable ticker at all for a given TAIFEX futures contract is itself
unverified — see `application.ports.yahoo_ticker_mapping.YahooTickerMappingRepository`'s
docstring; a missing mapping or an empty/error result from this port must always be
treated as "still a gap", never fabricated or guessed at.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class YahooBar:
    """One row of a `yfinance` `interval="1h"` history response, already normalized
    (column names, timezone, decimal conversion, dedup, ordering, finite-value checks —
    see `infrastructure.market_data.yfinance_history_adapter`) but not yet resolved into
    a domain `Bar` — that resolution needs `domain.trading_calendar.TradingCalendar.
    boundary_for_open` and an `InstrumentMasterEntry`, neither of which belongs in this
    Yahoo-shaped DTO."""

    at: datetime
    """Tz-aware, converted to Asia/Taipei by the adapter (`yfinance` itself returns the
    exchange-local timezone reported by Yahoo's own response metadata, which is not
    necessarily Asia/Taipei — never assumed, always converted). This codebase's own bar
    labeling convention is open-time (see `domain/bar.py`); whether Yahoo's own
    `interval="1h"` index labels a bar's open or close time is a stated assumption (open
    time), not a verified fact — see the module docstring."""
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class YahooHistoryQueryError(Exception):
    """Raised when a `yfinance` history query could not be completed after the
    adapter's own bounded retry/backoff — rate limit, timeout, network error, or a
    response shape the adapter doesn't recognize (schema change). Distinct from "Yahoo
    genuinely has no bars for this range" (an empty `Sequence[YahooBar]`, not an
    exception). Callers must treat this the same as an empty result: the requested range
    stays an unresolved gap, retried (if at all) only on a future trigger, never by
    looping synchronously here."""


class YahooHistoryQueryPort(Protocol):
    def query_1h_bars(
        self, *, yahoo_ticker: str, start_date: date, end_date: date
    ) -> Sequence[YahooBar]:
        """已收盤 1 小時 K 歷史查詢 (`yfinance`, `interval="1h"`, `auto_adjust=False`
        explicit). `[start_date, end_date]` is inclusive from this port's point of
        view — the adapter is responsible for translating that into `yfinance`'s own
        `end`-exclusive convention. Blocks the calling thread until the HTTP response
        arrives or the adapter's own bounded retry/backoff gives up — callers must never
        invoke this from a thread that must stay responsive (e.g. the `EventCoordinator`
        dispatch thread)."""
        ...
