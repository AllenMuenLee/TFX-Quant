"""Pure planning helpers for the two-month bar-history yfinance backfill (Feature 04
extension — see `docs/adr/0007-two-month-bar-history-persistence.md`'s yfinance
extension decision).

Kept as its own module rather than added to `bar_record.py` purely to avoid a
`bar_record` <-> `trading_calendar` import cycle (`trading_calendar.py` already imports
`bar_record.py` for `MarketSession`); this module needs neither, so it's free of that
constraint. Vendor-neutral by design — nothing here assumes a particular history-query
provider's per-call limits; the caller (`application.market_data.
bar_history_backfill_service.BarHistoryBackfillService`) supplies its own
`max_span_days`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date


def chunk_consecutive_days(
    missing_days: Sequence[date], max_span_days: int
) -> list[tuple[date, date]]:
    """Groups `missing_days` (must already be sorted ascending and deduped) into the
    minimum number of inclusive `(start, end)` calendar-date ranges, each spanning at
    most `max_span_days` calendar days end-to-end — a bounded batch size for a single
    history-query call, not a specific vendor's documented per-call limit. Days that
    aren't calendar-consecutive (a weekend/holiday sitting between two missing trading
    days) don't force a new chunk on their own, since a plain calendar-date `start`/`end`
    range naturally only yields bars for the trading days actually within it."""
    if not missing_days:
        return []
    if max_span_days < 1:
        raise ValueError(f"max_span_days must be >= 1, got {max_span_days}")

    chunks: list[tuple[date, date]] = []
    chunk_start = missing_days[0]
    chunk_end = missing_days[0]
    for day in missing_days[1:]:
        if (day - chunk_start).days < max_span_days:
            chunk_end = day
        else:
            chunks.append((chunk_start, chunk_end))
            chunk_start = day
            chunk_end = day
    chunks.append((chunk_start, chunk_end))
    return chunks
