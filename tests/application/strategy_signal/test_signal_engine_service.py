from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import (
    BarClosed,
    Event,
    ManualPositionSyncCompleted,
    MarketDataFreshnessChanged,
    PositionDiscrepancyDetected,
)
from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.strategy_signal.signal_engine_service import StrategySignalEngineService
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind
from tfx_quant.domain.position_reconciliation import DiscrepancyKind, ReconciliationTrigger
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_MA_WINDOW = 35
_FLAT_LOOKBACK = 5
_WARMUP = _MA_WINDOW + _FLAT_LOOKBACK - 1


class FakeEventBus:
    """Synchronous, in-process event bus — same shape as `tests/application/
    order_management/test_order_manager.py`'s `FakeEventBus`."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        self.published: list[Event] = []

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        self.published.append(event)
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in list(handlers):
                    handler(event)


class FakeClock:
    def __init__(self, now: Timestamp) -> None:
        self._now = now

    def now(self) -> Timestamp:
        return self._now


def _flat_position_lookup(*_args: object) -> NetPosition:
    return NetPosition(0)


def _bars(
    closes: list[str], *, opens: list[str] | None = None, final_start: datetime | None = None
) -> list[Bar]:
    n = len(closes)
    if opens is None:
        opens = list(closes)
    if final_start is None:
        final_start = datetime(2026, 9, 16, 9, 45, tzinfo=TAIPEI_TZ)
    base_start = final_start - timedelta(hours=n - 1)
    bars = []
    for i in range(n):
        start = base_start + timedelta(hours=i)
        end = start + timedelta(hours=1)
        o = Decimal(opens[i])
        c = Decimal(closes[i])
        bars.append(
            Bar(
                instrument=_INSTRUMENT,
                contract=_CONTRACT,
                open=Price(o),
                high=Price(max(o, c)),
                low=Price(min(o, c)),
                close=Price(c),
                volume=1,
                start=Timestamp(start),
                end=Timestamp(end),
            )
        )
    return bars


def _rising_warmup_plus_entry(
    *, extra_closes: list[str] | None = None, extra_opens: list[str] | None = None
) -> list[Bar]:
    """39-bar warm-up (arithmetic ramp, step 10 => MA slope UP, 5-MA range 40 => not
    choppy) plus two closing RED bars, closing at 10:45 — a full ENTER_LONG fixture."""
    warmup = [str(Decimal("10000") + Decimal("10") * i) for i in range(_WARMUP - 2)]
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    closes = warmup + tail_closes + (extra_closes or [])
    opens = warmup + tail_opens + (extra_opens or [])
    return _bars(closes, opens=opens)


def _service() -> tuple[
    StrategySignalEngineService, MockTradeGateway, SqliteOrderRepository, FakeEventBus, FakeClock
]:
    event_bus = FakeEventBus()
    gateway = MockTradeGateway(event_publisher=event_bus)
    repo = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    clock = FakeClock(Timestamp.now())
    order_manager = OrderManager(
        trade_gateway=gateway,
        order_repository=repo,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=_flat_position_lookup,
    )
    service = StrategySignalEngineService(
        order_manager=order_manager,
        order_repository=repo,
        clock=clock,
        event_bus=event_bus,
        selected_account=lambda: _ACCOUNT,
    )
    return service, gateway, repo, event_bus, clock


def _publish_bars(event_bus: FakeEventBus, bars: list[Bar]) -> None:
    for bar in bars:
        event_bus.publish(
            BarClosed(at=bar.end, instrument=bar.instrument, contract=bar.contract, bar=bar)
        )


# -- Bar -> decision -> order submission --------------------------------------------------


def test_entry_signal_submits_order_via_order_manager() -> None:
    service, gateway, _repo, event_bus, _clock = _service()
    bars = _rising_warmup_plus_entry()
    _publish_bars(event_bus, bars)

    assert len(gateway.submitted_orders) == 1
    order = gateway.submitted_orders[0]
    assert order.side is Side.BUY
    assert order.quantity.lots == 1
    assert order.kind is OrderKind.OPEN
    assert order.price == Price(Decimal("10380"))


def test_fill_confirmation_enables_add_on_signal() -> None:
    service, gateway, repo, event_bus, _clock = _service()
    bars = _rising_warmup_plus_entry()
    _publish_bars(event_bus, bars)
    entry_order = gateway.submitted_orders[0]

    gateway.simulate_ack(entry_order.client_order_id, "B0001")
    gateway.simulate_fill(entry_order.client_order_id, 1, Decimal("10380"), broker_fill_no="F1")

    add_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    _publish_bars(event_bus, [add_bar])

    assert len(gateway.submitted_orders) == 2
    add_order = gateway.submitted_orders[1]
    assert add_order.side is Side.BUY
    assert add_order.quantity.lots == 1
    assert add_order.price == Price(Decimal("10390"))


def test_replaying_the_same_closed_bar_does_not_double_submit() -> None:
    service, gateway, _repo, event_bus, _clock = _service()
    bars = _rising_warmup_plus_entry()
    _publish_bars(event_bus, bars)
    assert len(gateway.submitted_orders) == 1

    # Redeliver the exact same trigger bar (a duplicate event) — the intent_key-based
    # idempotency in OrderManager must return the existing intent, not submit again.
    event_bus.publish(
        BarClosed(at=bars[-1].end, instrument=_INSTRUMENT, contract=_CONTRACT, bar=bars[-1])
    )
    assert len(gateway.submitted_orders) == 1


# -- clear() resets engine state (BarSignalStateStore) --------------------------------------


def test_clear_resets_engine_requiring_a_fresh_warmup() -> None:
    service, gateway, _repo, event_bus, _clock = _service()
    bars = _rising_warmup_plus_entry()
    # Feed everything except the final trigger bar, so no order has been submitted yet.
    _publish_bars(event_bus, bars[:-1])
    assert len(gateway.submitted_orders) == 0

    service.clear(_INSTRUMENT, _CONTRACT)

    # Only the final (would-be trigger) bar survives the clear — nowhere near a full
    # 35-bar MA window any more, so it must not enter.
    _publish_bars(event_bus, [bars[-1]])
    assert len(gateway.submitted_orders) == 0


# -- Fail-closed gating from other features' events ------------------------------------------


def test_stale_market_data_blocks_entry_submission() -> None:
    service, gateway, _repo, event_bus, _clock = _service()
    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=_INSTRUMENT, contract=_CONTRACT, is_stale=True
        )
    )
    bars = _rising_warmup_plus_entry()
    _publish_bars(event_bus, bars)
    assert len(gateway.submitted_orders) == 0


def test_position_discrepancy_blocks_entry_until_manual_sync_completed() -> None:
    service, gateway, _repo, event_bus, _clock = _service()
    event_bus.publish(
        PositionDiscrepancyDetected(
            at=Timestamp.now(),
            trigger=ReconciliationTrigger.TIMED_POLL,
            account=_ACCOUNT,
            instrument=_INSTRUMENT,
            contract=_CONTRACT,
            expected_net=NetPosition(0),
            actual_net=NetPosition(1),
            discrepancy=DiscrepancyKind.QUANTITY,
            resulting_strategy_state=None,
            correlation_id="corr-1",
        )
    )
    bars = _rising_warmup_plus_entry()
    _publish_bars(event_bus, bars)
    assert len(gateway.submitted_orders) == 0

    event_bus.publish(
        ManualPositionSyncCompleted(
            at=Timestamp.now(),
            account=_ACCOUNT,
            instrument=_INSTRUMENT,
            contract=_CONTRACT,
            baseline_before=NetPosition(0),
            baseline_after=NetPosition(0),
            correlation_id="corr-1",
        )
    )
    next_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    _publish_bars(event_bus, [next_bar])
    assert len(gateway.submitted_orders) == 1


# -- 04:55 forced flatten via the clock-tick trigger ------------------------------------------


def test_clock_tick_submits_flatten_order_once_position_established() -> None:
    service, gateway, _repo, event_bus, _clock = _service()
    bars = _rising_warmup_plus_entry()
    _publish_bars(event_bus, bars)
    entry_order = gateway.submitted_orders[0]
    gateway.simulate_ack(entry_order.client_order_id, "B0001")
    gateway.simulate_fill(entry_order.client_order_id, 1, Decimal("10380"), broker_fill_no="F1")

    service.on_clock_tick(Timestamp(datetime(2026, 9, 17, 4, 55, tzinfo=TAIPEI_TZ)))

    assert len(gateway.submitted_orders) == 2
    flatten_order = gateway.submitted_orders[1]
    assert flatten_order.side is Side.SELL
    assert flatten_order.kind is OrderKind.CLOSE
    assert flatten_order.quantity.lots == 1
