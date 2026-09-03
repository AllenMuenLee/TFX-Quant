"""Aggregation of already-persisted live trades on an operator-confirmed grid."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.market_data import RecordedMarketEvent
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import Timestamp

Boundary = tuple[Timestamp, Timestamp, date, MarketSession]
BoundaryResolver = Callable[[Timestamp], Boundary | None]


@dataclass(frozen=True, slots=True)
class ClosedAggregation:
    bar: Bar
    trading_day: date
    session: MarketSession
    first_sequence: int
    last_sequence: int


@dataclass(slots=True)
class _Forming:
    boundary: Boundary
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    first_sequence: int
    last_sequence: int
    last_fingerprint: tuple[object, ...]
    last_total: int
    complete: bool = True


class RealtimeBarAggregator:
    """Conservative 60-minute aggregator; it never invents empty bars.

    ``boundary_resolver`` is mandatory because the quote PDF does not define futures
    sessions, trading-day assignment, or bar labels.  A missing boundary rejects the
    event from aggregation. Bars are emitted only after a later event (or ``advance``)
    proves the configured close boundary has passed.

    ``recording_started_at`` is when this aggregator's quote feed actually began
    delivering events. The boundary that was already in progress at that instant can
    only ever be observed from its middle, so its open/high/low and volume would be
    those of a fragment while `LocalClosedBarWriter` would still persist it as a
    complete bar and publish a `BarClosed` the strategy would treat as real. Such a
    boundary is therefore dropped whole: aggregation starts at the first boundary whose
    own open label is at or after ``recording_started_at`` (starting exactly on an open
    label keeps that boundary — nothing of it was missed). ``None`` disables the rule.
    """

    def __init__(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        boundary_resolver: BoundaryResolver,
        recording_started_at: Timestamp | None = None,
    ) -> None:
        self.instrument = instrument
        self.contract = contract
        self._resolve = boundary_resolver
        self._started_at = recording_started_at
        self._forming: _Forming | None = None

    @property
    def forming_bar(self) -> Bar | None:
        return None if self._forming is None else self._bar(self._forming)

    def accept(self, event: RecordedMarketEvent) -> list[ClosedAggregation]:
        if not event.is_trade or event.matched_at is None or event.match_price is None:
            return []
        if event.match_quantity is None or event.total_match_quantity is None:
            return []
        boundary = self._resolve(event.matched_at)
        if boundary is None:
            return []
        if self._started_at is not None and boundary[0].value < self._started_at.value:
            # A boundary already under way when recording began — see the class docstring.
            return []
        closed = self.advance(event.matched_at)
        fingerprint = (
            event.match_time_raw,
            event.match_price,
            event.match_quantity,
            event.total_match_quantity,
        )
        if self._forming is None:
            self._forming = _Forming(
                boundary,
                event.match_price,
                event.match_price,
                event.match_price,
                event.match_price,
                event.match_quantity,
                event.raw.sequence,
                event.raw.sequence,
                fingerprint,
                event.total_match_quantity,
            )
            return closed
        forming = self._forming
        if boundary[0] != forming.boundary[0]:
            # A different open boundary without the prior boundary being closable is a gap.
            forming.complete = False
            return closed
        if fingerprint == forming.last_fingerprint:
            return closed
        if (
            event.raw.sequence <= forming.last_sequence
            or event.total_match_quantity < forming.last_total
        ):
            forming.complete = False
            return closed
        forming.high = max(forming.high, event.match_price)
        forming.low = min(forming.low, event.match_price)
        forming.close = event.match_price
        forming.volume += event.match_quantity
        forming.last_sequence = event.raw.sequence
        forming.last_fingerprint = fingerprint
        forming.last_total = event.total_match_quantity
        return closed

    def advance(self, now: Timestamp) -> list[ClosedAggregation]:
        forming = self._forming
        if forming is None or now.value < forming.boundary[1].value:
            return []
        self._forming = None
        if not forming.complete:
            return []
        start, end, trading_day, session = forming.boundary
        return [
            ClosedAggregation(
                self._bar(forming),
                trading_day,
                session,
                forming.first_sequence,
                forming.last_sequence,
            )
        ]

    def mark_incomplete(self) -> None:
        if self._forming is not None:
            self._forming.complete = False

    def _bar(self, forming: _Forming) -> Bar:
        start, end, _day, _session = forming.boundary
        return Bar(
            self.instrument,
            self.contract,
            Price(forming.open),
            Price(forming.high),
            Price(forming.low),
            Price(forming.close),
            forming.volume,
            start,
            end,
        )
