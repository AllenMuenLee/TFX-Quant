"""BarAggregator — pure 60-minute OHLCV bar aggregation from a validated tick stream.

Two policies worth stating explicitly (see `docs/adr/0006-market-data-and-bar-
aggregation.md` for the full rationale):

- **Duplicate/late/out-of-order ticks are dropped, never amend a bar retroactively.**
  `Tick.serial_no` (SPARK API's real per-symbol trade sequence number — see
  `domain/tick.py`) must strictly increase within the forming bar; a tick that doesn't
  advance it is a duplicate or a late arrival and is ignored. A tick whose timestamp
  falls before the most recently *closed* bar's end is dropped outright — closed bars
  are final, emitted exactly once, and never revised.
- **A bar period with zero ticks is never synthesized.** `on_clock` advances the
  aggregator's notion of "now" without fabricating OHLC data that was never observed;
  a no-trade interval simply produces no `Bar` for that slot.
"""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.tick import Tick
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar
from tfx_quant.telemetry import get_logger, log_debug, log_info

_logger = get_logger(__name__)


@dataclass
class _FormingBar:
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int
    start: Timestamp
    end: Timestamp
    """The boundary this bar will close at once reached — see `Bar`'s module docstring
    for the label(=open)/close(=end) semantics this mirrors."""
    last_serial_no: int
    tick_count: int = 1
    """Accepted ticks only — duplicate/out-of-order drops never increment this. Logged
    on close as the bar's "tick count" (除錯日誌需求)."""


class BarAggregator:
    """One instance per (instrument, contract)."""

    def __init__(
        self,
        *,
        instrument: Instrument,
        contract: ContractMonth,
        entry: InstrumentMasterEntry,
        calendar: TradingCalendar,
    ) -> None:
        self._instrument = instrument
        self._contract = contract
        self._entry = entry
        self._calendar = calendar
        self._forming: _FormingBar | None = None
        self._last_closed_end: Timestamp | None = None

    def on_tick(self, tick: Tick) -> list[Bar]:
        if self._last_closed_end is not None and tick.at.value < self._last_closed_end.value:
            log_debug(
                _logger,
                "tick_dropped_late_for_closed_bar",
                instrument=self._instrument.value,
                contract=self._contract.code,
                tick_serial_no=tick.serial_no,
                tick_at=tick.at.value.isoformat(),
                last_closed_end=self._last_closed_end.value.isoformat(),
            )
            return []  # late arrival for an already-closed, final bar — dropped

        closed = self._advance_to(tick.at)

        if self._forming is None:
            boundary = self._calendar.boundary_containing(tick.at, self._entry)
            if boundary is None:
                return closed  # outside any session — cannot be assigned a bar
            open_ts, close_ts = boundary
            self._forming = _FormingBar(
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.size,
                start=open_ts,
                end=close_ts,
                last_serial_no=tick.serial_no,
            )
            return closed

        if tick.serial_no <= self._forming.last_serial_no:
            log_debug(
                _logger,
                "tick_dropped_duplicate_or_out_of_order",
                instrument=self._instrument.value,
                contract=self._contract.code,
                tick_serial_no=tick.serial_no,
                forming_last_serial_no=self._forming.last_serial_no,
            )
            return closed  # duplicate / late / out-of-order within the current bar

        if tick.price.amount > self._forming.high.amount:
            self._forming.high = tick.price
        if tick.price.amount < self._forming.low.amount:
            self._forming.low = tick.price
        self._forming.close = tick.price
        self._forming.volume += tick.size
        self._forming.last_serial_no = tick.serial_no
        self._forming.tick_count += 1
        return closed

    def on_clock(self, now: Timestamp) -> list[Bar]:
        """Call periodically (no tick required) so a forming bar with trades still
        closes promptly at its boundary during a lull, and so a silent no-trade slot
        still advances past without ever being fabricated."""
        return self._advance_to(now)

    def forming_bar_snapshot(self) -> Bar | None:
        f = self._forming
        if f is None:
            return None
        return Bar(
            instrument=self._instrument,
            contract=self._contract,
            open=f.open,
            high=f.high,
            low=f.low,
            close=f.close,
            volume=f.volume,
            start=f.start,
            end=f.end,
        )

    def _advance_to(self, at: Timestamp) -> list[Bar]:
        if self._forming is not None and at.value >= self._forming.end.value:
            return [self._close_forming()]
        return []

    def _close_forming(self) -> Bar:
        f = self._forming
        assert f is not None
        bar = Bar(
            instrument=self._instrument,
            contract=self._contract,
            open=f.open,
            high=f.high,
            low=f.low,
            close=f.close,
            volume=f.volume,
            start=f.start,
            end=f.end,
        )
        self._last_closed_end = f.end
        self._forming = None
        log_info(
            _logger,
            "bar_closed",
            instrument=self._instrument.value,
            contract=self._contract.code,
            start=bar.start.value.isoformat(),
            end=bar.end.value.isoformat(),
            open=str(bar.open.amount),
            high=str(bar.high.amount),
            low=str(bar.low.amount),
            close=str(bar.close.amount),
            volume=bar.volume,
            tick_count=f.tick_count,
            candle_color=bar.candle_color.value,
        )
        return bar


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
