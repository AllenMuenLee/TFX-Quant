"""Integration coverage for the implementation prompt's harder acceptance scenarios:
"重連期間晚到成交" (a fill arrives, discovered only via reconciliation, after the
broker connection is re-established) and "狀態查詢矛盾" (the broker's reconciliation
query flatly contradicts what this system believes locally). Both must resolve without
ever resending/resubmitting an order — `OrderManager`'s own timeout/reconciliation logic
already guarantees this; this file proves it holds end-to-end when the trigger is
`ConnectivityMonitor`'s reconnect flow, not a bare `BrokerSessionReady` publish.

Wires `ConnectivityMonitor`, its `ConnectivityTrackingTradeGateway`, and `OrderManager`
together against one shared event bus — the same composition order
`desktop/composition.py` uses (`OrderManager` subscribes its own `BrokerSessionReady`
handler first; `ConnectivityMonitor.attach_reconnect_reconciliation_watcher()` is called
last)."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import SecretStr

from tfx_quant.application.connectivity.connectivity_monitor import ConnectivityMonitor
from tfx_quant.application.connectivity.gateway_tracking import ConnectivityTrackingTradeGateway
from tfx_quant.application.events.events import BrokerSessionInvalidated, Event
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Price(Decimal("18500"))


class FakeEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in list(handlers):
                    handler(event)


class FakeClock:
    def __init__(self, now: Timestamp) -> None:
        self._now = now

    def now(self) -> Timestamp:
        return self._now


class _InertCancellable:
    def cancel(self) -> None:
        pass


class _InertScheduler:
    """Captures every scheduled reconnect attempt but never fires it — this test drives
    the reconnect attempt explicitly (`session.start(...)`) rather than through the
    backoff timer (already covered by `test_connectivity_monitor.py`), so a real
    `threading.Timer` here would just be a stray background thread with nothing to
    verify."""

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _InertCancellable:
        return _InertCancellable()


def _flat_position_lookup(
    _account: TradingAccount, _instrument: Instrument, _contract: ContractMonth
) -> NetPosition:
    return NetPosition(0)


def _login_request() -> LoginRequest:
    return LoginRequest(
        environment=Environment.TEST, user_id="F00000000012345678", password=SecretStr("x")
    )


@dataclass
class _Rig:
    event_bus: FakeEventBus
    clock: FakeClock
    raw_gateway: MockTradeGateway
    session: MockBrokerSession
    order_repository: SqliteOrderRepository
    order_manager: OrderManager
    connectivity_monitor: ConnectivityMonitor
    state_machine: StrategyStateMachine


def _build_rig() -> _Rig:
    event_bus = FakeEventBus()
    clock = FakeClock(Timestamp.now())
    raw_gateway = MockTradeGateway(event_publisher=event_bus)
    session = MockBrokerSession(event_publisher=event_bus)
    state_machine = StrategyStateMachine()
    state_machine.transition(StrategyState.STARTING)
    state_machine.transition(StrategyState.RUNNING)

    # `ConnectivityMonitor` is built against the raw session/gateway, same order
    # `desktop/composition.py` uses — see that module's own comment.
    connectivity_monitor = ConnectivityMonitor(
        broker_session=session,
        strategy_state_machine=state_machine,
        clock=clock,
        event_bus=event_bus,
        scheduler=_InertScheduler(),
    )
    gateway = ConnectivityTrackingTradeGateway(raw_gateway, connectivity_monitor)

    order_repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    order_manager = OrderManager(
        trade_gateway=gateway,
        order_repository=order_repository,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=_flat_position_lookup,
    )
    # Must run after `order_manager` (and, in the real composition root,
    # `PositionReconciliationService`/`BarHistoryBackfillService`) have subscribed their
    # own `BrokerSessionReady` handlers — see `ConnectivityMonitor.
    # attach_reconnect_reconciliation_watcher`'s docstring.
    connectivity_monitor.attach_reconnect_reconciliation_watcher()
    connectivity_monitor.remember_login_request(_login_request())

    return _Rig(
        event_bus=event_bus,
        clock=clock,
        raw_gateway=raw_gateway,
        session=session,
        order_repository=order_repository,
        order_manager=order_manager,
        connectivity_monitor=connectivity_monitor,
        state_machine=state_machine,
    )


def _submit_and_ack(rig: _Rig, *, idempotency_key: str) -> Any:
    intent = rig.order_manager.submit(
        OrderRequest(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            side=Side.BUY,
            quantity=Quantity(1),
            price=_PRICE,
            kind=OrderKind.OPEN,
            time_in_force=TimeInForce.ROD,
            idempotency_key=idempotency_key,
            workflow_id="wf-1",
            reason="test",
        )
    )
    rig.raw_gateway.simulate_ack(intent.client_order_id, "B-1", seq=1)
    return rig.order_repository.find_by_client_order_id(intent.client_order_id)


def test_fill_that_arrives_only_during_reconnect_reconciliation_resolves_without_resend() -> None:
    rig = _build_rig()
    acknowledged = _submit_and_ack(rig, idempotency_key="late-fill")
    assert acknowledged.status is OrderStatus.ACKNOWLEDGED

    rig.event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="模擬斷線"))
    assert rig.connectivity_monitor.is_reconnecting is True
    assert rig.connectivity_monitor.current_pause() is not None

    # The fill never arrived live (the whole point of the scenario) — only discoverable
    # via the broker's queryable fill set once reconnected.
    rig.raw_gateway.set_fills(
        [
            Fill(
                client_order_id=acknowledged.client_order_id,
                instrument=Instrument.MXF,
                side=Side.BUY,
                quantity=Quantity(1),
                price=_PRICE,
                at=Timestamp.now(),
                broker_fill_no="F-late",
                broker_seq_no=2,
            )
        ]
    )

    rig.session.start(_login_request())  # the reconnect attempt (mock happy path)

    resolved = rig.order_repository.find_by_client_order_id(acknowledged.client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.FILLED
    assert resolved.filled_quantity == 1
    # The critical safety property: never resent/resubmitted.
    assert len(rig.raw_gateway.submitted_orders) == 1
    assert rig.connectivity_monitor.is_reconnecting is False
    record = rig.connectivity_monitor.current_pause()
    assert record is not None
    assert record.reconciled is True


def test_contradictory_reconciliation_query_leaves_the_order_unknown_never_resent() -> None:
    rig = _build_rig()
    acknowledged = _submit_and_ack(rig, idempotency_key="contradictory-query")
    assert acknowledged.status is OrderStatus.ACKNOWLEDGED

    rig.event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="模擬斷線"))

    # ACKNOWLEDGED -> REJECTED is not a legal order-report transition (see
    # `domain.order_state_machine._LEGAL_ORDER_REPORT_TRANSITIONS`) — a broker query
    # flatly contradicting the locally-believed state.
    rig.raw_gateway.set_order_reports(
        [
            OrderReport(
                client_order_id=acknowledged.client_order_id,
                status=OrderStatus.REJECTED,
                broker_seq_no=2,
                at=Timestamp.now(),
                reject_reason="矛盾查詢結果",
            )
        ]
    )

    rig.session.start(_login_request())

    resolved = rig.order_repository.find_by_client_order_id(acknowledged.client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.UNKNOWN
    assert len(rig.raw_gateway.submitted_orders) == 1  # never resent
