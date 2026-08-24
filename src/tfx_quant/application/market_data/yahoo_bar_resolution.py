"""resolve_yahoo_bar — the one place a `yfinance`-sourced `YahooBar` row is turned into
a domain `Bar` plus its canonical trading-day/session, shared by both writers of bar
history: `MarketDataBarService` (frequent poll, near-real-time) and
`BarHistoryBackfillService` (coarser two-month gap-fill sweep) — see each module's own
docstring for how they differ. Extracted here purely so the boundary/session resolution
and OHLCV validation isn't duplicated between the two.

Deliberately returns a resolved `Bar` even for a still-*forming* boundary (`is_forming`)
rather than treating that as a resolution failure — `MarketDataBarService` needs exactly
that row for its `forming_bar()` UI snapshot; `BarHistoryBackfillService` simply skips it
(a still-forming boundary is never something a gap-fill sweep should write).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tfx_quant.application.ports.yahoo_history_query import YahooBar
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar


@dataclass(frozen=True, slots=True)
class ResolvedYahooBar:
    bar: Bar
    trading_day: date
    session: MarketSession
    is_forming: bool
    """`True` when this boundary's close is still in the future as of `now` — the row
    is this contract's currently-forming bar, not a closed one. Callers must never
    persist a `ResolvedYahooBar` with `is_forming=True` as a closed `BarRecord`."""


def resolve_yahoo_bar(
    *,
    instrument: Instrument,
    contract: ContractMonth,
    entry: InstrumentMasterEntry,
    calendar: TradingCalendar,
    yahoo_bar: YahooBar,
    now: Timestamp,
) -> ResolvedYahooBar | None:
    """`None` means this row cannot be resolved onto this codebase's own canonical
    60-minute bar grid at all — an off-grid open timestamp (not exactly one of
    `TradingCalendar.bar_boundaries()`'s own outputs), a timestamp matching no trading
    day/session, or invalid OHLCV — and must be treated as an unresolved gap, never
    coerced or guessed at."""
    try:
        open_ts = Timestamp(yahoo_bar.at)
    except DomainError:
        return None

    boundary = calendar.boundary_for_open(open_ts, entry)
    if boundary is None:
        return None
    boundary_open, boundary_close = boundary

    context = calendar.session_context_for(boundary_open, entry)
    if context is None:
        return None
    trading_day, session = context

    try:
        bar = Bar(
            instrument=instrument,
            contract=contract,
            open=Price(yahoo_bar.open),
            high=Price(yahoo_bar.high),
            low=Price(yahoo_bar.low),
            close=Price(yahoo_bar.close),
            volume=yahoo_bar.volume,
            start=boundary_open,
            end=boundary_close,
        )
    except DomainError:
        return None

    return ResolvedYahooBar(
        bar=bar,
        trading_day=trading_day,
        session=session,
        is_forming=boundary_close.value > now.value,
    )
