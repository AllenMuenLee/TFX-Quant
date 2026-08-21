from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tfx_quant.application.events.events import (
    Event,
    ReversalCompleted,
    ReversalEntrySubmitted,
    ReversalFlatConfirmed,
    ReversalPausedSafe,
    ReversalWorkflowStarted,
    ReverseEntryBlocked,
)
from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.reversal_scaling.errors import ReversalAlreadyActiveError
from tfx_quant.application.reversal_scaling.reversal_service import ReversalWorkflowService
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderIntent, OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.reversal_workflow import (
    ReversalWorkflowId,
    ReversalWorkflowRecord,
    ReversalWorkflowState,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind, StrategySignal
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository
from tfx_quant.persistence.sqlite_reversal_workflow_repository import (
    SqliteReversalWorkflowRepository,
)

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Price(Decimal("18500"))
_NEAR_EOD = Timestamp(datetime(2026, 9, 16, 4, 50, tzinfo=TAIPEI_TZ))


class FakeEventBus:
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

    def advance(self, seconds: float) -> None:
        self._now = Timestamp(self._now.value + timedelta(seconds=seconds))


def _flat_position_lookup(
    _account: TradingAccount, _instrument: Instrument, _contract: ContractMonth
) -> NetPosition:
    return NetPosition(0)


def _signal() -> StrategySignal:
    return StrategySignal(
        kind=SignalKind.REVERSE,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        at=Timestamp.now(),
        reason="test reversal",
    )


def _position(lots: int) -> Position:
    return Position(
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        net=NetPosition(lots),
        average_price=None if lots == 0 else _PRICE,
        as_of=Timestamp.now(),
    )


def _filled_order_intent(
    *,
    client_order_id: ClientOrderId,
    side: Side,
    kind: OrderKind,
    quantity_lots: int,
    workflow_id: str,
) -> OrderIntent:
    now = Timestamp.now()
    return OrderIntent(
        local_order_id=LocalOrderId(),
        client_order_id=client_order_id,
        workflow_id=workflow_id,
        idempotency_key=f"{workflow_id}:{kind.value}",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=side,
        kind=kind,
        quantity=Quantity(quantity_lots),
        status=OrderStatus.FILLED,
        created_at=now,
        updated_at=now,
        filled_quantity=quantity_lots,
        avg_fill_price=_PRICE,
    )


@dataclass
class Harness:
    service: ReversalWorkflowService
    gateway: MockTradeGateway
    order_repo: SqliteOrderRepository
    reversal_repo: SqliteReversalWorkflowRepository
    order_manager: OrderManager
    clock: FakeClock
    event_bus: FakeEventBus


def _setup(
    *,
    positions: tuple[Position, ...] = (),
    now: Timestamp | None = None,
    order_timeout_seconds: float = 30.0,
    session_healthy: bool = True,
    market_data_healthy: bool = True,
    eod_margin: timedelta = timedelta(minutes=10),
) -> Harness:
    event_bus = FakeEventBus()
    gateway = MockTradeGateway(event_publisher=event_bus, positions=list(positions))
    order_repo = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    reversal_repo = SqliteReversalWorkflowRepository(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    clock = FakeClock(now if now is not None else Timestamp.now())
    order_manager = OrderManager(
        trade_gateway=gateway,
        order_repository=order_repo,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=_flat_position_lookup,
        order_timeout_seconds=order_timeout_seconds,
    )
    service = ReversalWorkflowService(
        order_manager=order_manager,
        order_repository=order_repo,
        reversal_workflow_repository=reversal_repo,
        trade_gateway=gateway,
        clock=clock,
        event_bus=event_bus,
        session_healthy=lambda: session_healthy,
        market_data_healthy=lambda i, c: market_data_healthy,
        eod_margin=eod_margin,
    )
    return Harness(service, gateway, order_repo, reversal_repo, order_manager, clock, event_bus)


def _entry_orders(gateway: MockTradeGateway) -> list[Any]:
    return [o for o in gateway.submitted_orders if o.kind is OrderKind.OPEN]


# -- Happy path ---------------------------------------------------------------------------


def test_reversal_from_short_two_lots_completes() -> None:
    h = _setup(positions=(_position(-2),))
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    assert record.state is ReversalWorkflowState.CLOSE_ORDER_SUBMITTED
    assert record.reversal_side is Side.BUY
    close_id = record.close_client_order_id
    assert close_id is not None
    close_order = next(o for o in h.gateway.submitted_orders if o.client_order_id == close_id)
    assert close_order.quantity == Quantity(2)
    assert close_order.kind is OrderKind.CLOSE

    h.clock.advance(3600)
    h.gateway.set_positions(())  # broker now confirms flat
    h.gateway.simulate_ack(close_id, "B-close")
    h.gateway.simulate_fill(close_id, 2, Decimal("18500"), broker_fill_no="F-close")

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.ENTRY_ORDER_SUBMITTED
    entry_id = resolved.entry_client_order_id
    assert entry_id is not None
    entry_order = next(o for o in h.gateway.submitted_orders if o.client_order_id == entry_id)
    assert entry_order.side is Side.BUY
    assert entry_order.quantity == Quantity(1)
    assert entry_order.kind is OrderKind.OPEN

    h.gateway.simulate_ack(entry_id, "B-entry")
    h.gateway.simulate_fill(entry_id, 1, Decimal("18500"), broker_fill_no="F-entry")
    final = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert final is not None
    assert final.state is ReversalWorkflowState.COMPLETED
    assert len(h.gateway.submitted_orders) == 2


def test_reversal_from_long_one_lot_completes() -> None:
    h = _setup(positions=(_position(1),))
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    assert record.reversal_side is Side.SELL
    close_id = record.close_client_order_id
    assert close_id is not None

    h.gateway.set_positions(())
    h.gateway.simulate_ack(close_id, "B-close")
    h.gateway.simulate_fill(close_id, 1, Decimal("18500"), broker_fill_no="F-close")

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.ENTRY_ORDER_SUBMITTED
    assert resolved.entry_client_order_id is not None
    entry_order = next(
        o for o in h.gateway.submitted_orders if o.client_order_id == resolved.entry_client_order_id
    )
    assert entry_order.side is Side.SELL


# -- Event-timestamp ordering (cross-cutting acceptance requirement) ----------------------


def test_reverse_entry_submitted_timestamp_is_after_fill_and_flat_confirmation() -> None:
    h = _setup(positions=(_position(-1),))
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    close_id = record.close_client_order_id
    assert close_id is not None

    h.clock.advance(3600)  # guarantee every subsequent service timestamp is unambiguously later
    h.gateway.set_positions(())
    h.gateway.simulate_ack(close_id, "B-close")
    fill = h.gateway.simulate_fill(close_id, 1, Decimal("18500"), broker_fill_no="F-close")

    entry_submitted = next(
        e for e in h.event_bus.published if isinstance(e, ReversalEntrySubmitted)
    )
    flat_confirmed = next(e for e in h.event_bus.published if isinstance(e, ReversalFlatConfirmed))
    assert entry_submitted.at.value > fill.at.value
    assert entry_submitted.at.value >= flat_confirmed.at.value


# -- Partial fill then disconnect (OrderManager's own timeout drives the pause) -----------


def test_partial_fill_then_disconnect_pauses_no_entry() -> None:
    h = _setup(positions=(_position(-2),), order_timeout_seconds=10.0)
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    close_id = record.close_client_order_id
    assert close_id is not None
    h.gateway.simulate_ack(close_id, "B-close")
    h.gateway.simulate_fill(close_id, 1, Decimal("18500"), broker_fill_no="F-partial")  # short of 2

    h.clock.advance(20.0)
    h.order_manager.on_clock_tick()

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.PAUSED_SAFE
    assert _entry_orders(h.gateway) == []
    assert any(isinstance(e, ReversalPausedSafe) for e in h.event_bus.published)


def test_late_fill_after_pause_does_not_resurrect_workflow() -> None:
    h = _setup(positions=(_position(-1),), order_timeout_seconds=10.0)
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    close_id = record.close_client_order_id
    assert close_id is not None

    h.clock.advance(20.0)
    h.order_manager.on_clock_tick()  # times out to UNKNOWN, reversal pauses
    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.PAUSED_SAFE

    # The order itself can still resolve later at the OrderManager level...
    h.gateway.simulate_fill(close_id, 1, Decimal("18500"), broker_fill_no="F-late")
    order = h.order_repo.find_by_client_order_id(close_id)
    assert order is not None
    assert order.status is OrderStatus.FILLED

    # ...but the reversal workflow itself must stay frozen, never resubmitting an entry.
    still_paused = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert still_paused is not None
    assert still_paused.state is ReversalWorkflowState.PAUSED_SAFE
    assert _entry_orders(h.gateway) == []


# -- Fully filled but position still nonzero (contradiction) ------------------------------


def test_fully_filled_but_position_still_nonzero_pauses() -> None:
    h = _setup(positions=(_position(-1),))
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    close_id = record.close_client_order_id
    assert close_id is not None

    # Deliberately do NOT clear the scripted position — the broker's query still shows -1
    # even though the fill report claims full close.
    h.gateway.simulate_ack(close_id, "B-close")
    h.gateway.simulate_fill(close_id, 1, Decimal("18500"), broker_fill_no="F-close")

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.PAUSED_SAFE
    assert _entry_orders(h.gateway) == []
    blocked_events = [e for e in h.event_bus.published if isinstance(e, ReverseEntryBlocked)]
    assert len(blocked_events) == 1
    assert blocked_events[0].result is not None
    assert blocked_events[0].result.is_flat is False


# -- Reject ---------------------------------------------------------------------------------


def test_reject_pauses_immediately_no_entry() -> None:
    h = _setup(positions=(_position(-1),))
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    close_id = record.close_client_order_id
    assert close_id is not None
    h.gateway.simulate_reject(close_id, "資金不足")

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.PAUSED_SAFE
    assert _entry_orders(h.gateway) == []


# -- Already flat / EOD margin ---------------------------------------------------------------


def test_already_flat_blocks_with_no_orders() -> None:
    h = _setup(positions=())
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    assert record.state is ReversalWorkflowState.BLOCKED
    assert record.pause_reason == "已無持倉，無需反手"
    assert h.gateway.submitted_orders == []


def test_too_close_to_eod_blocks_before_any_order() -> None:
    h = _setup(positions=(_position(-1),), now=_NEAR_EOD)
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    assert record.state is ReversalWorkflowState.BLOCKED
    assert h.gateway.submitted_orders == []
    blocked_events = [e for e in h.event_bus.published if isinstance(e, ReverseEntryBlocked)]
    assert len(blocked_events) == 1
    assert blocked_events[0].result is None


# -- One workflow per contract / idempotency ---------------------------------------------


def test_second_reversal_while_active_raises() -> None:
    h = _setup(positions=(_position(-1),))
    h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    with pytest.raises(ReversalAlreadyActiveError):
        h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t2")


def test_duplicate_trigger_key_returns_existing_no_second_workflow() -> None:
    h = _setup(positions=(_position(-1),))
    first = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    second = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    assert first.workflow_id == second.workflow_id
    assert len(h.gateway.submitted_orders) == 1


def test_workflow_started_event_published_once() -> None:
    h = _setup(positions=(_position(-1),))
    h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    started_events = [e for e in h.event_bus.published if isinstance(e, ReversalWorkflowStarted)]
    assert len(started_events) == 1


# -- Manual intervention: paused workflows never auto-resume ------------------------------


def test_paused_workflow_never_auto_resumes() -> None:
    h = _setup(positions=(_position(-1),))
    record = h.service.start_reversal(_signal(), account=_ACCOUNT, price=_PRICE, trigger_key="t1")
    close_id = record.close_client_order_id
    assert close_id is not None
    h.gateway.simulate_reject(close_id, "資金不足")

    h.service.resume_pending_workflows()

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.PAUSED_SAFE
    assert _entry_orders(h.gateway) == []


# -- Restart at every step ------------------------------------------------------------------


def test_resume_from_position_queried_submits_close_order() -> None:
    h = _setup(positions=(_position(-1),))
    now = Timestamp.now()
    record = ReversalWorkflowRecord(
        workflow_id=ReversalWorkflowId(),
        trigger_key="t1",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        state=ReversalWorkflowState.POSITION_QUERIED,
        created_at=now,
        updated_at=now,
        price=_PRICE,
        starting_net=NetPosition(-1),
        reversal_side=Side.BUY,
    )
    h.reversal_repo.save(record)

    h.service.resume_pending_workflows()

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.CLOSE_ORDER_SUBMITTED
    assert len(h.gateway.submitted_orders) == 1


def test_resume_from_close_order_submitted_already_filled_advances() -> None:
    h = _setup(positions=(_position(-1),))
    now = Timestamp.now()
    workflow_id = ReversalWorkflowId()
    close_id = ClientOrderId()
    order = _filled_order_intent(
        client_order_id=close_id,
        side=Side.BUY,
        kind=OrderKind.CLOSE,
        quantity_lots=1,
        workflow_id=str(workflow_id.value),
    )
    h.order_repo.save_intent(order)
    record = ReversalWorkflowRecord(
        workflow_id=workflow_id,
        trigger_key="t1",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        state=ReversalWorkflowState.CLOSE_ORDER_SUBMITTED,
        created_at=now,
        updated_at=now,
        price=_PRICE,
        starting_net=NetPosition(-1),
        reversal_side=Side.BUY,
        close_client_order_id=close_id,
    )
    h.reversal_repo.save(record)
    h.gateway.set_positions(())  # flat by the time we resume

    h.service.resume_pending_workflows()

    resolved = h.reversal_repo.find_by_workflow_id(workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.ENTRY_ORDER_SUBMITTED
    assert len(h.gateway.submitted_orders) == 1  # only the entry — close was never resent


def test_resume_from_flat_confirmed_submits_entry_order() -> None:
    h = _setup(positions=())
    now = Timestamp.now()
    record = ReversalWorkflowRecord(
        workflow_id=ReversalWorkflowId(),
        trigger_key="t1",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        state=ReversalWorkflowState.FLAT_CONFIRMED,
        created_at=now,
        updated_at=now,
        price=_PRICE,
        starting_net=NetPosition(-1),
        reversal_side=Side.BUY,
    )
    h.reversal_repo.save(record)

    h.service.resume_pending_workflows()

    resolved = h.reversal_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.ENTRY_ORDER_SUBMITTED
    assert len(h.gateway.submitted_orders) == 1

    # A second resume must never resubmit — the entry order is merely "still in flight".
    h.service.resume_pending_workflows()
    assert len(h.gateway.submitted_orders) == 1


def test_resume_from_entry_order_submitted_already_filled_completes() -> None:
    h = _setup(positions=())
    now = Timestamp.now()
    workflow_id = ReversalWorkflowId()
    entry_id = ClientOrderId()
    order = _filled_order_intent(
        client_order_id=entry_id,
        side=Side.BUY,
        kind=OrderKind.OPEN,
        quantity_lots=1,
        workflow_id=str(workflow_id.value),
    )
    h.order_repo.save_intent(order)
    record = ReversalWorkflowRecord(
        workflow_id=workflow_id,
        trigger_key="t1",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        state=ReversalWorkflowState.ENTRY_ORDER_SUBMITTED,
        created_at=now,
        updated_at=now,
        price=_PRICE,
        starting_net=NetPosition(-1),
        reversal_side=Side.BUY,
        entry_client_order_id=entry_id,
    )
    h.reversal_repo.save(record)

    h.service.resume_pending_workflows()

    resolved = h.reversal_repo.find_by_workflow_id(workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.COMPLETED
    assert any(isinstance(e, ReversalCompleted) for e in h.event_bus.published)


def test_resume_from_close_order_submitted_rejected_pauses_and_blocks_entry() -> None:
    h = _setup(positions=(_position(-1),))
    now = Timestamp.now()
    workflow_id = ReversalWorkflowId()
    close_id = ClientOrderId()
    rejected_order = OrderIntent(
        local_order_id=LocalOrderId(),
        client_order_id=close_id,
        workflow_id=str(workflow_id.value),
        idempotency_key=f"{workflow_id.value}:close",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=Side.BUY,
        kind=OrderKind.CLOSE,
        quantity=Quantity(1),
        status=OrderStatus.REJECTED,
        created_at=now,
        updated_at=now,
        reject_reason="資金不足",
    )
    h.order_repo.save_intent(rejected_order)
    record = ReversalWorkflowRecord(
        workflow_id=workflow_id,
        trigger_key="t1",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        state=ReversalWorkflowState.CLOSE_ORDER_SUBMITTED,
        created_at=now,
        updated_at=now,
        price=_PRICE,
        starting_net=NetPosition(-1),
        reversal_side=Side.BUY,
        close_client_order_id=close_id,
    )
    h.reversal_repo.save(record)

    h.service.resume_pending_workflows()

    resolved = h.reversal_repo.find_by_workflow_id(workflow_id)
    assert resolved is not None
    assert resolved.state is ReversalWorkflowState.PAUSED_SAFE
    assert _entry_orders(h.gateway) == []
    assert any(isinstance(e, ReverseEntryBlocked) for e in h.event_bus.published)
