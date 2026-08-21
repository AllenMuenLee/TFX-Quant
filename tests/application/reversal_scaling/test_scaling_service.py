from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from tfx_quant.application.events.events import Event
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.reversal_scaling.errors import InvalidSignalKindError
from tfx_quant.application.reversal_scaling.scaling_service import ScalingService
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind, StrategySignal
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Price(Decimal("18500"))
_MID_MORNING = Timestamp(datetime(2026, 9, 16, 10, 45, tzinfo=TAIPEI_TZ))
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


def _signal(kind: SignalKind) -> StrategySignal:
    return StrategySignal(
        kind=kind, instrument=Instrument.MXF, contract=_CONTRACT, at=Timestamp.now(), reason="test"
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


def _flat_position_lookup(
    _account: TradingAccount, _instrument: Instrument, _contract: ContractMonth
) -> NetPosition:
    return NetPosition(0)


def _setup(
    *, positions: tuple[Position, ...] = (), now: Timestamp = _MID_MORNING
) -> tuple[ScalingService, MockTradeGateway, SqliteOrderRepository, OrderManager, FakeClock]:
    event_bus = FakeEventBus()
    gateway = MockTradeGateway(event_publisher=event_bus, positions=positions)
    repo = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    clock = FakeClock(now)
    order_manager = OrderManager(
        trade_gateway=gateway,
        order_repository=repo,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=_flat_position_lookup,
    )
    scaling = ScalingService(
        order_manager=order_manager, order_repository=repo, trade_gateway=gateway, clock=clock
    )
    return scaling, gateway, repo, order_manager, clock


# -- Allowed path ------------------------------------------------------------------------


def test_scaling_allowed_submits_exactly_one_order() -> None:
    scaling, gateway, _repo, _om, _clock = _setup(positions=(_position(1),))
    decision = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_LONG), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert decision.allowed is True
    assert decision.order_intent is not None
    assert decision.order_intent.status is OrderStatus.SUBMITTING
    assert decision.order_intent.side is Side.BUY
    assert decision.order_intent.quantity == Quantity(1)
    assert len(gateway.submitted_orders) == 1


def test_scaling_allowed_for_short_position_add_short() -> None:
    scaling, gateway, _repo, _om, _clock = _setup(positions=(_position(-1),))
    decision = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_SHORT), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert decision.allowed is True
    assert decision.order_intent is not None
    assert decision.order_intent.side is Side.SELL


# -- Gate rejections ------------------------------------------------------------------------


def test_scaling_rejects_when_position_is_not_exactly_one_lot() -> None:
    scaling, gateway, _repo, _om, _clock = _setup(positions=())  # flat, net == 0
    decision = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_LONG), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert decision.allowed is False
    assert decision.reason is not None
    assert "±1" in decision.reason
    assert len(gateway.submitted_orders) == 0


def test_scaling_rejects_when_active_order_exists() -> None:
    scaling, gateway, repo, order_manager, clock = _setup(positions=(_position(1),))
    # Occupy the contract with an unrelated active order first.
    order_manager.submit(
        OrderRequest(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            side=Side.BUY,
            quantity=Quantity(1),
            price=_PRICE,
            kind=OrderKind.OPEN,
            time_in_force=TimeInForce.ROD,
            idempotency_key="unrelated",
            workflow_id="wf",
            reason="setup",
        )
    )
    decision = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_LONG), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert decision.allowed is False
    assert decision.reason is not None
    assert "委託" in decision.reason
    assert len(gateway.submitted_orders) == 1  # only the unrelated setup order


def test_scaling_rejects_mismatched_signal_direction() -> None:
    scaling, gateway, _repo, _om, _clock = _setup(positions=(_position(1),))
    decision = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_SHORT), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert decision.allowed is False
    assert len(gateway.submitted_orders) == 0


def test_scaling_rejects_too_close_to_eod() -> None:
    scaling, gateway, _repo, _om, _clock = _setup(positions=(_position(1),), now=_NEAR_EOD)
    decision = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_LONG), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert decision.allowed is False
    assert decision.reason is not None
    assert "04:55" in decision.reason
    assert len(gateway.submitted_orders) == 0


def test_scaling_rejects_wrong_signal_kind() -> None:
    scaling, _gateway, _repo, _om, _clock = _setup(positions=(_position(1),))
    with pytest.raises(InvalidSignalKindError):
        scaling.evaluate_and_submit(
            _signal(SignalKind.EXIT_ALL), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
        )


# -- Idempotency: repeated bar must not resend -------------------------------------------


def test_duplicate_idempotency_key_never_resends() -> None:
    scaling, gateway, _repo, _om, _clock = _setup(positions=(_position(1),))
    first = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_LONG), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    second = scaling.evaluate_and_submit(
        _signal(SignalKind.ADD_LONG), account=_ACCOUNT, price=_PRICE, idempotency_key="scale-1"
    )
    assert first.order_intent is not None
    assert second.order_intent is not None
    assert first.order_intent.local_order_id == second.order_intent.local_order_id
    assert len(gateway.submitted_orders) == 1
