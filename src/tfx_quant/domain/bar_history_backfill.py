"""Pure planning helpers for the two-month bar-history vendor backfill (Feature 04
extension — 元大 SPARK API `GetKLine` 60分K query, see
`docs/adr/0007-two-month-bar-history-persistence.md`'s extension decision).

Kept as its own module rather than added to `bar_record.py` purely to avoid a
`bar_record` <-> `trading_calendar` import cycle (`trading_calendar.py` already imports
`bar_record.py` for `MarketSession`); this module needs neither, so it's free of that
constraint.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date


def chunk_consecutive_days(
    missing_days: Sequence[date], max_span_days: int
) -> list[tuple[date, date]]:
    """Groups `missing_days` (must already be sorted ascending and deduped) into the
    minimum number of inclusive `(start, end)` calendar-date ranges, each spanning at
    most `max_span_days` calendar days end-to-end — the vendor's own `GetKLine`
    per-call limit for intraday periods (60分k: 5天/次, per the docs' own K線種類查詢
    限制 table). Days that aren't calendar-consecutive (a weekend/holiday sitting
    between two missing trading days) don't force a new chunk on their own, since
    `GetKLine`'s `SDate`/`EDate` range is a plain calendar-date span and the vendor
    itself only returns bars for the trading days actually within it."""
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
