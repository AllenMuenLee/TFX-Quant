from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import (
    Event,
    LatestPriceObserved,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
)
from tfx_quant.application.trade_reports.position_valuation_service import (
    PositionValuationService,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trade_report import LedgerFill, PositionEffect
from tfx_quant.domain.valuation import PriceQuality
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository

_CONTRACT = ContractMonth(2026, 9)
_MULT = Decimal("50")
_DAY = date(2026, 8, 25)


class FakeEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)
        return lambda: None

    def publish(self, event: Event) -> None:
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in handlers:
                    handler(event)


class MovingClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> Timestamp:
        return Timestamp(self._now)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _ts(hour: int, minute: int = 0) -> Timestamp:
    return Timestamp(datetime(2026, 8, 25, hour, minute, tzinfo=TAIPEI_TZ))


def _fill(fill_id: str, side: Side, qty: int, price: str, effect: PositionEffect) -> LedgerFill:
    return LedgerFill(
        fill_id=fill_id,
        broker_order_no=f"B-{fill_id}",
        order_correlation=f"wf-{fill_id}",
        masked_account="***4567",
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=side,
        position_effect=effect,
        quantity=qty,
        price=Decimal(price),
        filled_at=_ts(10),
        trading_day=_DAY,
        commission=Decimal("0"),
        tax=Decimal("0"),
        source="SIMULATION",
        simulation=True,
    )


def _service(clock: MovingClock, bus: FakeEventBus) -> tuple[PositionValuationService, Any]:
    ledger = SqliteFillLedgerRepository(sqlite3.connect(":memory:", check_same_thread=False))
    svc = PositionValuationService(
        fill_ledger=ledger,
        multiplier_lookup=lambda _i, _c: _MULT,
        clock=clock,
        event_bus=bus,
        simulation=True,
        stale_after_seconds=90.0,
    )
    return svc, ledger


def test_flat_book_reports_zero_unrealized_and_realized_from_closed_trades() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("open", Side.BUY, 1, "100", PositionEffect.OPEN))
    ledger.append(_fill("close", Side.SELL, 1, "110", PositionEffect.CLOSE))

    snap = svc.snapshot()

    assert snap.open_positions == ()
    assert snap.realized_pnl == Decimal("500")  # (110-100) * 50
    assert snap.unrealized_pnl == Decimal("0")
    assert snap.total_pnl == Decimal("500")
    assert snap.simulation is True


def test_open_long_marked_from_the_feed_gives_unrealized_and_total() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("o1", Side.BUY, 2, "100", PositionEffect.OPEN))
    bus.publish(
        LatestPriceObserved(
            at=_ts(11),
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            price=Decimal("130"),
            observed_at=_ts(11),
            quality="OK",
        )
    )

    snap = svc.snapshot()

    assert len(snap.open_positions) == 1
    pos = snap.open_positions[0]
    assert pos.net_lots == 2
    assert pos.avg_cost == Decimal("100")
    assert pos.price_quality is PriceQuality.OK
    assert pos.unrealized_pnl == Decimal("3000")  # (130-100) * 50 * 2
    assert snap.unrealized_pnl == Decimal("3000")
    assert snap.total_pnl == Decimal("3000")


def test_short_position_unrealized_sign() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("s1", Side.SELL, 1, "100", PositionEffect.OPEN))
    bus.publish(
        LatestPriceObserved(
            at=_ts(11),
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            price=Decimal("90"),
            observed_at=_ts(11),
            quality="OK",
        )
    )

    snap = svc.snapshot()

    assert snap.open_positions[0].net_lots == -1
    assert snap.open_positions[0].unrealized_pnl == Decimal("500")  # short profits as price falls


def test_no_mark_yet_is_unavailable_and_suppresses_total() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("o1", Side.BUY, 1, "100", PositionEffect.OPEN))

    snap = svc.snapshot()

    assert snap.open_positions[0].price_quality is PriceQuality.UNAVAILABLE
    assert snap.open_positions[0].unrealized_pnl is None
    assert snap.unrealized_pnl is None
    assert snap.total_pnl is None


def test_stale_price_never_values_the_position() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("o1", Side.BUY, 1, "100", PositionEffect.OPEN))
    bus.publish(
        LatestPriceObserved(
            at=_ts(11),
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            price=Decimal("130"),
            observed_at=_ts(11),
            quality="OK",
        )
    )
    clock.advance(120)  # older than stale_after_seconds

    snap = svc.snapshot()

    assert snap.open_positions[0].price_quality is PriceQuality.STALE
    assert snap.open_positions[0].mark_price == Decimal("130")  # shown, but not used
    assert snap.open_positions[0].unrealized_pnl is None
    assert snap.total_pnl is None


def test_freshness_stale_event_marks_the_price_stale() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("o1", Side.BUY, 1, "100", PositionEffect.OPEN))
    bus.publish(
        LatestPriceObserved(
            at=_ts(11),
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            price=Decimal("130"),
            observed_at=_ts(11),
            quality="OK",
        )
    )
    bus.publish(
        MarketDataFreshnessChanged(
            at=_ts(11), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )

    assert svc.snapshot().open_positions[0].price_quality is PriceQuality.STALE

    bus.publish(
        MarketDataFreshnessChanged(
            at=_ts(11), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=False
        )
    )
    assert svc.snapshot().open_positions[0].price_quality is PriceQuality.OK


def test_gap_marks_the_price_gapped_until_cleared() -> None:
    clock = MovingClock(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))
    bus = FakeEventBus()
    svc, ledger = _service(clock, bus)
    ledger.append(_fill("o1", Side.BUY, 1, "100", PositionEffect.OPEN))
    bus.publish(
        LatestPriceObserved(
            at=_ts(11),
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            price=Decimal("130"),
            observed_at=_ts(11),
            quality="OK",
        )
    )
    bus.publish(
        MarketDataGapDetected(
            at=_ts(11), instrument=Instrument.MXF, contract=_CONTRACT, reason="reconnect"
        )
    )
    assert svc.snapshot().open_positions[0].price_quality is PriceQuality.GAP

    bus.publish(MarketDataGapCleared(at=_ts(11), instrument=Instrument.MXF, contract=_CONTRACT))
    assert svc.snapshot().open_positions[0].price_quality is PriceQuality.OK
