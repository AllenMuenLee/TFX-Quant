"""PositionValuationService — realized + unrealized + total P&L for the open book.

Open-lot cost basis comes from the *same* FIFO pass (`_fifo.match_fills`) that produces
realized trades, so realized and unrealized are one calculation path. The mark price is
the latest `LatestPriceObserved` for that market from the real quote feed; when it is
stale, gapped, missing, or the feed is disconnected, the position's unrealized P&L is
`None` and the snapshot total is `None` — no synthesized price is ever substituted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    Event,
    LatestPriceObserved,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
)
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.fill_ledger_repository import FillLedgerRepository
from tfx_quant.application.trade_reports._fifo import OpenLot, match_fills
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.valuation import OpenPositionValuation, PriceQuality, ValuationSnapshot

_D = Decimal("0")
_FULL_RANGE = (date(1, 1, 1), date(9999, 12, 31))

MultiplierLookup = Callable[[Instrument, ContractMonth], Decimal]
_Key = tuple[Instrument, ContractMonth]


class EventBus(Protocol):
    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


@dataclass(slots=True)
class _Mark:
    price: Decimal
    observed_at: Timestamp
    reported_quality: str


@dataclass(frozen=True, slots=True)
class _AggregatedPosition:
    net_lots: int
    avg_cost: Decimal


class PositionValuationService:
    def __init__(
        self,
        *,
        fill_ledger: FillLedgerRepository,
        multiplier_lookup: MultiplierLookup,
        clock: Clock,
        event_bus: EventBus,
        simulation: bool,
        stale_after_seconds: float = 90.0,
    ) -> None:
        self._fills = fill_ledger
        self._multiplier = multiplier_lookup
        self._clock = clock
        self._simulation = simulation
        self._stale_after = stale_after_seconds
        self._marks: dict[_Key, _Mark] = {}
        self._stale: set[_Key] = set()
        self._gapped: set[_Key] = set()
        event_bus.subscribe(LatestPriceObserved, self._on_price)
        event_bus.subscribe(MarketDataFreshnessChanged, self._on_freshness)
        event_bus.subscribe(MarketDataGapDetected, self._on_gap)
        event_bus.subscribe(MarketDataGapCleared, self._on_gap_cleared)

    # -- event handlers ---------------------------------------------------------------

    def _on_price(self, event: LatestPriceObserved) -> None:
        self._marks[(event.instrument, event.contract)] = _Mark(
            price=event.price, observed_at=event.observed_at, reported_quality=event.quality
        )

    def _on_freshness(self, event: MarketDataFreshnessChanged) -> None:
        key = (event.instrument, event.contract)
        if event.is_stale:
            self._stale.add(key)
        else:
            self._stale.discard(key)

    def _on_gap(self, event: MarketDataGapDetected) -> None:
        self._gapped.add((event.instrument, event.contract))

    def _on_gap_cleared(self, event: MarketDataGapCleared) -> None:
        self._gapped.discard((event.instrument, event.contract))

    # -- read model -----------------------------------------------------------------

    def snapshot(self, *, trading_day_range: tuple[date, date] | None = None) -> ValuationSnapshot:
        start, end = trading_day_range or _FULL_RANGE
        fills = tuple(self._fills.list_between(start, end))
        multipliers: dict[tuple[object, object], Decimal] = {}
        for fill in fills:
            pair = (fill.instrument, fill.contract)
            if pair not in multipliers:
                multipliers[pair] = self._multiplier(fill.instrument, fill.contract)
        result = match_fills(fills, multipliers)
        realized = sum((t.net_pnl for t in result.realized_trades), _D)

        now = self._clock.now()
        positions = tuple(
            self._value(instrument, contract, aggregated, now)
            for (instrument, contract), aggregated in _aggregate(result.open_lots).items()
        )
        if not positions:
            return ValuationSnapshot(
                as_of=now,
                realized_pnl=realized,
                unrealized_pnl=_D,
                total_pnl=realized,
                open_positions=(),
                simulation=self._simulation,
            )
        if any(p.unrealized_pnl is None for p in positions):
            unrealized: Decimal | None = None
        else:
            unrealized = sum((p.unrealized_pnl or _D for p in positions), _D)
        total = None if unrealized is None else realized + unrealized
        return ValuationSnapshot(
            as_of=now,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=total,
            open_positions=positions,
            simulation=self._simulation,
        )

    def _value(
        self,
        instrument: Instrument,
        contract: ContractMonth,
        aggregated: _AggregatedPosition,
        now: Timestamp,
    ) -> OpenPositionValuation:
        multiplier = self._multiplier(instrument, contract)
        mark = self._marks.get((instrument, contract))
        quality = self._quality(instrument, contract, mark, now)
        if quality is PriceQuality.OK and mark is not None:
            unrealized: Decimal | None = (
                (mark.price - aggregated.avg_cost) * aggregated.net_lots * multiplier
            )
            mark_price: Decimal | None = mark.price
        else:
            unrealized = None
            mark_price = mark.price if mark is not None else None
        return OpenPositionValuation(
            instrument=instrument,
            contract=contract,
            net_lots=aggregated.net_lots,
            avg_cost=aggregated.avg_cost,
            multiplier=multiplier,
            mark_price=mark_price,
            unrealized_pnl=unrealized,
            price_quality=quality,
            last_price_at=None if mark is None else mark.observed_at,
        )

    def _quality(
        self, instrument: Instrument, contract: ContractMonth, mark: _Mark | None, now: Timestamp
    ) -> PriceQuality:
        key = (instrument, contract)
        if mark is None:
            return PriceQuality.UNAVAILABLE
        if key in self._gapped or mark.reported_quality == "GAP":
            return PriceQuality.GAP
        age = (now.value - mark.observed_at.value).total_seconds()
        if key in self._stale or mark.reported_quality == "STALE" or age > self._stale_after:
            return PriceQuality.STALE
        return PriceQuality.OK


def _aggregate(open_lots: tuple[OpenLot, ...]) -> dict[_Key, _AggregatedPosition]:
    grouped: dict[_Key, list[OpenLot]] = {}
    for lot in open_lots:
        grouped.setdefault((lot.instrument, lot.contract), []).append(lot)
    aggregated: dict[_Key, _AggregatedPosition] = {}
    for key, lots in grouped.items():
        total_lots = sum(lot.remaining for lot in lots)
        if total_lots == 0:
            continue
        notional = sum((lot.open_price * lot.remaining for lot in lots), _D)
        sign = 1 if lots[0].side is Side.BUY else -1
        aggregated[key] = _AggregatedPosition(
            net_lots=sign * total_lots, avg_cost=notional / total_lots
        )
    return aggregated


__all__ = ["PositionValuationService"]
