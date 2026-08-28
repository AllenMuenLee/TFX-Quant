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
    BarClosed,
    EodFlattenCompleted,
    EodFlattenPausedSafe,
    EodFlattenWorkflowStarted,
    Event,
    StartupPositionSafetyPauseTriggered,
)
from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.risk.errors import (
    EodFlattenAlreadyActiveError,
    StaleEmergencyConfirmationError,
)
from tfx_quant.application.risk.risk_supervisor import RiskSupervisor
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderIntent, OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.risk import EodFlattenWorkflowState
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_eod_flatten_workflow_repository import (
    SqliteEodFlattenWorkflowRepository,
)
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_INSTRUMENT = Instrument.MXF
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Decimal("18500")


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

    def set(self, now: Timestamp) -> None:
        self._now = now


class FakeSelection:
    instrument = _INSTRUMENT
    contract = _CONTRACT


def _flat_position_lookup(*_args: object) -> NetPosition:
    return NetPosition(0)


def _at(hour: int, minute: int, *, day: int = 16) -> Timestamp:
    return Timestamp(datetime(2026, 9, day, hour, minute, tzinfo=TAIPEI_TZ))


def _position(lots: int) -> Position:
    return Position(
        account=_ACCOUNT,
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        net=NetPosition(lots),
        average_price=None if lots == 0 else Price(_PRICE),
        as_of=Timestamp.now(),
    )


def _bar_closed(at: Timestamp) -> BarClosed:
    bar = Bar(
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        start=Timestamp(at.value - timedelta(hours=1)),
        end=at,
        open=Price(_PRICE),
        high=Price(_PRICE),
        low=Price(_PRICE),
        close=Price(_PRICE),
        volume=0,
    )
    return BarClosed(at=at, instrument=_INSTRUMENT, contract=_CONTRACT, bar=bar)


def _active_open_order(*, status: OrderStatus = OrderStatus.ACKNOWLEDGED) -> OrderIntent:
    now = Timestamp.now()
    return OrderIntent(
        local_order_id=LocalOrderId(),
        client_order_id=ClientOrderId(),
        workflow_id="unrelated-workflow",
        idempotency_key="unrelated-key",
        account=_ACCOUNT,
        instrument=_INSTRUMENT,
        contract=_CONTRACT,
        side=Side.BUY,
        kind=OrderKind.OPEN,
        quantity=Quantity(1),
        status=status,
        created_at=now,
        updated_at=now,
    )


@dataclass
class Harness:
    supervisor: RiskSupervisor
    gateway: MockTradeGateway
    order_repo: SqliteOrderRepository
    eod_repo: SqliteEodFlattenWorkflowRepository
    order_manager: OrderManager
    clock: FakeClock
    event_bus: FakeEventBus
    strategy_state_machine: StrategyStateMachine


def _setup(
    *,
    positions: tuple[Position, ...] = (),
    now: Timestamp | None = None,
    session_healthy: bool = True,
    market_data_healthy: bool = True,
    initial_strategy_state: StrategyState = StrategyState.RUNNING,
) -> Harness:
    event_bus = FakeEventBus()
    gateway = MockTradeGateway(event_publisher=event_bus, positions=list(positions))
    order_repo = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    eod_repo = SqliteEodFlattenWorkflowRepository(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    clock = FakeClock(now if now is not None else _at(13, 0))
    order_manager = OrderManager(
        trade_gateway=gateway,
        order_repository=order_repo,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=_flat_position_lookup,
    )
    strategy_state_machine = StrategyStateMachine()
    if initial_strategy_state is not StrategyState.STOPPED:
        strategy_state_machine.transition(StrategyState.STARTING)
        if initial_strategy_state is not StrategyState.STARTING:
            strategy_state_machine.transition(StrategyState.RUNNING)
    supervisor = RiskSupervisor(
        order_manager=order_manager,
        order_repository=order_repo,
        eod_flatten_workflow_repository=eod_repo,
        trade_gateway=gateway,
        clock=clock,
        event_bus=event_bus,
        strategy_state_machine=strategy_state_machine,
        session_healthy=lambda: session_healthy,
        market_data_healthy=lambda i, c: market_data_healthy,
        current_selection=lambda: FakeSelection(),
        selected_account=lambda: _ACCOUNT,
    )
    return Harness(
        supervisor,
        gateway,
        order_repo,
        eod_repo,
        order_manager,
        clock,
        event_bus,
        strategy_state_machine,
    )


# -- validate_entry_window -----------------------------------------------------------------


def test_validate_entry_window_blocks_inside_band() -> None:
    h = _setup(now=_at(9, 45))
    assert h.supervisor.validate_entry_window() is not None


def test_validate_entry_window_allows_outside_band() -> None:
    h = _setup(now=_at(13, 0))
    assert h.supervisor.validate_entry_window() is None


# -- Scheduled 04:55 flatten ------------------------------------------------------------------


def test_scheduled_flatten_two_lots_completes() -> None:
    h = _setup(positions=(_position(-2),), now=_at(4, 50))
    h.supervisor.on_clock_tick()  # seed "outside band" observation
    h.event_bus.publish(_bar_closed(h.clock.now()))

    h.clock.set(_at(4, 55))
    h.supervisor.on_clock_tick()

    active = h.eod_repo.list_active()
    assert len(active) == 1
    record = active[0]
    assert record.state is EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED
    assert record.close_side is Side.BUY
    close_id = record.close_client_order_id
    assert close_id is not None
    close_order = next(o for o in h.gateway.submitted_orders if o.client_order_id == close_id)
    assert close_order.quantity == Quantity(2)
    assert close_order.kind is OrderKind.CLOSE

    h.gateway.set_positions(())
    h.gateway.simulate_ack(close_id, "B-close")
    h.gateway.simulate_fill(close_id, 2, _PRICE, broker_fill_no="F-close")

    resolved = h.eod_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is EodFlattenWorkflowState.COMPLETED
    assert any(isinstance(e, EodFlattenCompleted) for e in h.event_bus.published)


def test_scheduled_flatten_started_by_boundary_edge_only_once_per_day() -> None:
    h = _setup(positions=(_position(-1),), now=_at(4, 50))
    h.supervisor.on_clock_tick()
    h.event_bus.publish(_bar_closed(h.clock.now()))
    h.clock.set(_at(4, 56))
    h.supervisor.on_clock_tick()
    assert len(h.eod_repo.list_active()) == 1

    h.clock.set(_at(5, 10))
    h.supervisor.on_clock_tick()
    assert len(h.eod_repo.list_active()) == 1  # no duplicate workflow


def test_scheduled_flatten_when_already_flat_marks_already_flat_not_close() -> None:
    h = _setup(positions=(), now=_at(4, 50))
    h.supervisor.on_clock_tick()
    h.clock.set(_at(4, 56))
    h.supervisor.on_clock_tick()

    trigger_date = h.clock.now().value.date().isoformat()
    trigger_key = f"scheduled:{_INSTRUMENT.value}:{_CONTRACT.code}:{trigger_date}"
    record = h.eod_repo.find_by_trigger_key(trigger_key)
    assert record is not None
    assert record.state is EodFlattenWorkflowState.ALREADY_FLAT
    assert h.gateway.submitted_orders == []


def test_scheduled_flatten_skipped_when_process_starts_inside_band() -> None:
    """The implementation prompt's explicit "若程式在 04:55 之後啟動且有持倉，保持安全暫停並
    提示人工執行緊急平倉" — never auto-flatten on the very first observation."""
    h = _setup(positions=(_position(1),), now=_at(6, 0))
    h.supervisor.on_clock_tick()
    assert h.eod_repo.list_active() == []

    h.clock.set(_at(6, 5))
    h.supervisor.on_clock_tick()
    assert h.eod_repo.list_active() == []


def test_scheduled_flatten_waits_for_active_orders_then_proceeds() -> None:
    h = _setup(positions=(_position(1),), now=_at(4, 50))
    h.order_repo.save_intent(_active_open_order())
    h.supervisor.on_clock_tick()
    h.event_bus.publish(_bar_closed(h.clock.now()))

    h.clock.set(_at(4, 56))
    h.supervisor.on_clock_tick()
    active = h.eod_repo.list_active()
    assert len(active) == 1
    assert active[0].state is EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS
    assert h.gateway.submitted_orders == []

    # The unrelated active order clears (goes CANCELLED, a terminal state) -> next tick
    # should proceed to actually query position + submit the close order.
    unrelated = h.order_repo.list_active()[0]
    h.order_repo.update_intent(_replace_status(unrelated, OrderStatus.CANCELLED))
    h.supervisor.on_clock_tick()
    active = h.eod_repo.list_active()
    assert active[0].state is EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED


def _replace_status(order: OrderIntent, status: OrderStatus) -> OrderIntent:
    from dataclasses import replace

    return replace(order, status=status)


def test_close_order_rejected_pauses_workflow() -> None:
    h = _setup(positions=(_position(1),), now=_at(4, 50))
    h.supervisor.on_clock_tick()
    h.event_bus.publish(_bar_closed(h.clock.now()))
    h.clock.set(_at(4, 56))
    h.supervisor.on_clock_tick()

    record = h.eod_repo.list_active()[0]
    close_id = record.close_client_order_id
    assert close_id is not None
    h.gateway.simulate_reject(close_id, "insufficient margin")

    resolved = h.eod_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is EodFlattenWorkflowState.PAUSED_SAFE
    assert any(isinstance(e, EodFlattenPausedSafe) for e in h.event_bus.published)


def test_final_confirmation_mismatch_pauses_never_reports_complete() -> None:
    """ "不得宣稱已平倉" — a fill report alone is never enough; the final broker position
    query must independently confirm exactly zero."""
    h = _setup(positions=(_position(1),), now=_at(4, 50))
    h.supervisor.on_clock_tick()
    h.event_bus.publish(_bar_closed(h.clock.now()))
    h.clock.set(_at(4, 56))
    h.supervisor.on_clock_tick()

    record = h.eod_repo.list_active()[0]
    close_id = record.close_client_order_id
    assert close_id is not None
    # Broker's position query still (contradictorily) reports the position open even
    # though the close order's fill report says filled.
    h.gateway.simulate_ack(close_id, "B-close")
    h.gateway.simulate_fill(close_id, 1, _PRICE, broker_fill_no="F-close")

    resolved = h.eod_repo.find_by_workflow_id(record.workflow_id)
    assert resolved is not None
    assert resolved.state is EodFlattenWorkflowState.PAUSED_SAFE
    assert not any(isinstance(e, EodFlattenCompleted) for e in h.event_bus.published)


# -- Startup-after-04:55 safety check -----------------------------------------------------


def test_startup_with_position_inside_band_pauses_without_auto_flattening() -> None:
    h = _setup(positions=(_position(1),), now=_at(6, 0))
    h.event_bus.publish(BrokerSessionReadyStub(_ACCOUNT, h.clock.now()))

    assert h.eod_repo.list_active() == []
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE
    assert any(isinstance(e, StartupPositionSafetyPauseTriggered) for e in h.event_bus.published)


def test_startup_flat_position_inside_band_does_not_pause() -> None:
    h = _setup(positions=(), now=_at(6, 0))
    h.event_bus.publish(BrokerSessionReadyStub(_ACCOUNT, h.clock.now()))

    assert h.strategy_state_machine.state is StrategyState.RUNNING
    assert not any(
        isinstance(e, StartupPositionSafetyPauseTriggered) for e in h.event_bus.published
    )


def BrokerSessionReadyStub(account: TradingAccount, at: Timestamp) -> Any:
    from tfx_quant.application.events.events import BrokerSessionReady

    return BrokerSessionReady(at=at, account=account)


# -- Emergency flatten -----------------------------------------------------------------------


def test_emergency_flatten_pauses_strategy_and_submits_close() -> None:
    h = _setup(positions=(_position(2),), now=_at(13, 0))
    h.event_bus.publish(_bar_closed(h.clock.now()))

    record = h.supervisor.trigger_emergency_flatten(
        account=_ACCOUNT, instrument=_INSTRUMENT, contract=_CONTRACT, confirmed_net=NetPosition(2)
    )
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE
    assert record.state is EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED
    assert record.close_side is Side.SELL
    assert any(isinstance(e, EodFlattenWorkflowStarted) for e in h.event_bus.published)


def test_emergency_flatten_stale_confirmation_raises_and_starts_nothing() -> None:
    h = _setup(positions=(_position(2),), now=_at(13, 0))
    with pytest.raises(StaleEmergencyConfirmationError):
        h.supervisor.trigger_emergency_flatten(
            account=_ACCOUNT,
            instrument=_INSTRUMENT,
            contract=_CONTRACT,
            confirmed_net=NetPosition(1),
        )
    assert h.eod_repo.list_active() == []
    # The strategy is still forced into a safe pause even though the confirmation was
    # stale — an emergency-flatten attempt always pauses first.
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE


def test_emergency_flatten_already_active_raises() -> None:
    h = _setup(positions=(_position(1),), now=_at(13, 0))
    h.event_bus.publish(_bar_closed(h.clock.now()))
    h.supervisor.trigger_emergency_flatten(
        account=_ACCOUNT, instrument=_INSTRUMENT, contract=_CONTRACT, confirmed_net=NetPosition(1)
    )
    with pytest.raises(EodFlattenAlreadyActiveError):
        h.supervisor.trigger_emergency_flatten(
            account=_ACCOUNT,
            instrument=_INSTRUMENT,
            contract=_CONTRACT,
            confirmed_net=NetPosition(1),
        )
