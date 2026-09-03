"""CandleStreakCounter — 紅K/黑K consecutive-streak tracking over closed bars.

Bars are assembled from real-time Yuanta quote events by `application.market_data.
realtime_bar_aggregator.RealtimeBarAggregator` (see `desktop.quote_runtime.
QuoteRuntime`). This module only tracks the red/black streak over whatever closed
`Bar`s it's fed, independent of how they arrived.
"""

from __future__ import annotations

from tfx_quant.domain.bar import Bar, CandleColor


class CandleStreakCounter:
    """紅K/黑K 連續計數 — a DOJI resets the streak to zero, per the acceptance
    criterion. Pure/stateful, no I/O; fed one closed `Bar` at a time."""

    def __init__(self) -> None:
        self._color: CandleColor | None = None
        self._length = 0

    @property
    def color(self) -> CandleColor | None:
        """`None` when the streak is empty (nothing closed yet, or last reset by a
        DOJI)."""
        return self._color

    @property
    def length(self) -> int:
        return self._length

    def on_bar_closed(self, bar: Bar) -> None:
        color = bar.candle_color
        if color is CandleColor.DOJI:
            self._color = None
            self._length = 0
            return
        if color == self._color:
            self._length += 1
        else:
            self._color = color
            self._length = 1
