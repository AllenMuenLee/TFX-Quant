from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from tfx_quant.application.events.events import Event, OrderRequiresManualReview
from tfx_quant.application.order_management.errors import (
    ActiveWorkflowInProgressError,
    OrderExposureExceededError,
    OrderNotFoundError,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Price(Decimal("18500"))


class FakeEventBus:
    """Synchronous, in-process event bus — `publish()` calls every matching handler
    immediately, same shape as `tests/application/market_data/test_bar_service.py`'s
    `FakeEventBus`."""

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


def _request(
    *,
    idempotency_key: str = "key-1",
    workflow_id: str = "wf-1",
    side: Side = Side.BUY,
    quantity: int = 1,
    account: TradingAccount = _ACCOUNT,
    instrument: Instrument = Instrument.MXF,
    contract: ContractMonth = _CONTRACT,
    kind: OrderKind = OrderKind.OPEN,
) -> OrderRequest:
    return OrderRequest(
        account=account,
        instrument=instrument,
        contract=contract,
        side=side,
        quantity=Quantity(quantity),
        price=_PRICE,
        kind=kind,
        time_in_force=TimeInForce.ROD,
        idempotency_key=idempotency_key,
        workflow_id=workflow_id,
        reason="test",
    )


def _repo() -> SqliteOrderRepository:
    return SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))


def _manager(
    *,
    repo: SqliteOrderRepository | None = None,
    gateway: MockTradeGateway | None = None,
    event_bus: FakeEventBus | None = None,
    clock: FakeClock | None = None,
    position_lookup: (
        Callable[[TradingAccount, Instrument, ContractMonth], NetPosition] | None
    ) = None,
    order_timeout_seconds: float = 30.0,
) -> tuple[OrderManager, MockTradeGateway, SqliteOrderRepository, FakeEventBus]:
    event_bus = event_bus if event_bus is not None else FakeEventBus()
    gateway = gateway if gateway is not None else MockTradeGateway(event_publisher=event_bus)
    repo = repo if repo is not None else _repo()
    clock = clock if clock is not None else FakeClock(Timestamp.now())
    manager = OrderManager(
        trade_gateway=gateway,
        order_repository=repo,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=position_lookup or _flat_position_lookup,
        order_timeout_seconds=order_timeout_seconds,
    )
    return manager, gateway, repo, event_bus


# -- Normal fill / synchronous callback -------------------------------------------------


def test_normal_fill_reaches_filled_with_correct_avg_price() -> None:
    manager, gateway, repo, _bus = _manager()
    intent = manager.submit(_request())
    client_order_id = intent.client_order_id

    gateway.simulate_ack(client_order_id, "B0001")
    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")

    resolved = repo.find_by_client_order_id(client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.FILLED
    assert resolved.filled_quantity == 1
    assert resolved.avg_fill_price == Price(Decimal("18500"))
    assert len(gateway.submitted_orders) == 1


def test_report_arrives_before_submit_call_returns() -> None:
    """回報先於函式返回: the mock's on_submit callback fires synchronously inside
    submit_order, before OrderManager.submit() gets control back."""
    event_bus = FakeEventBus()

    def on_submit(gw: MockTradeGateway, order: Any, client_order_id: Any) -> None:
        gw.simulate_ack(client_order_id, "B0001")
        gw.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")

    gateway = MockTradeGateway(event_publisher=event_bus, on_submit=on_submit)
    manager, _gw, repo, _bus = _manager(gateway=gateway, event_bus=event_bus)

    intent = manager.submit(_request())

    resolved = repo.find_by_client_order_id(intent.client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.FILLED
    assert len(gateway.submitted_orders) == 1


# -- Partial fill --------------------------------------------------------------------------


def test_partial_then_full_fill() -> None:
    manager, gateway, repo, _bus = _manager()
    intent = manager.submit(_request(quantity=2))
    client_order_id = intent.client_order_id
    gateway.simulate_ack(client_order_id, "B0001")

    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")
    mid = repo.find_by_client_order_id(client_order_id)
    assert mid is not None
    assert mid.status is OrderStatus.PARTIALLY_FILLED
    assert mid.filled_quantity == 1

    gateway.simulate_fill(client_order_id, 1, Decimal("18600"), broker_fill_no="F2")
    final = repo.find_by_client_order_id(client_order_id)
    assert final is not None
    assert final.status is OrderStatus.FILLED
    assert final.filled_quantity == 2


# -- Reject ------------------------------------------------------------------------------


def test_reject_frees_the_contract_for_a_new_submission() -> None:
    manager, gateway, repo, event_bus = _manager()
    intent = manager.submit(_request(idempotency_key="key-1"))
    gateway.simulate_reject(intent.client_order_id, "資金不足")

    resolved = repo.find_by_client_order_id(intent.client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.REJECTED
    assert any(isinstance(e, OrderRequiresManualReview) for e in event_bus.published)

    # a fresh idempotency key for a new logical decision is now allowed
    second = manager.submit(_request(idempotency_key="key-2"))
    assert second.status is OrderStatus.SUBMITTING
    assert len(gateway.submitted_orders) == 2


# -- Timeout + late fill ----------------------------------------------------------------


def test_timeout_marks_unknown_then_late_fill_still_applies() -> None:
    clock = FakeClock(Timestamp.now())
    manager, gateway, repo, event_bus = _manager(clock=clock, order_timeout_seconds=10.0)
    intent = manager.submit(_request())
    client_order_id = intent.client_order_id

    clock.advance(20.0)
    manager.on_clock_tick()

    timed_out = repo.find_by_client_order_id(client_order_id)
    assert timed_out is not None
    assert timed_out.status is OrderStatus.UNKNOWN
    assert any(isinstance(e, OrderRequiresManualReview) for e in event_bus.published)

    # late fill still gets recorded — never resent
    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")
    final = repo.find_by_client_order_id(client_order_id)
    assert final is not None
    assert final.status is OrderStatus.FILLED
    assert len(gateway.submitted_orders) == 1


# -- Duplicate fill ------------------------------------------------------------------------


def test_duplicate_fill_is_ignored() -> None:
    manager, gateway, repo, _bus = _manager()
    intent = manager.submit(_request())
    client_order_id = intent.client_order_id
    gateway.simulate_ack(client_order_id, "B0001")
    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1", seq=10)

    gateway.replay_last_fill()

    resolved = repo.find_by_client_order_id(client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.FILLED
    assert resolved.filled_quantity == 1  # not double-counted


# -- Out-of-order order reports --------------------------------------------------------------


def test_out_of_order_order_report_is_ignored() -> None:
    manager, gateway, repo, _bus = _manager()
    intent = manager.submit(_request())
    client_order_id = intent.client_order_id
    gateway.simulate_ack(client_order_id, "B0001", seq=5)

    # a stale report claiming CANCELLED, sequenced before the ack already applied
    gateway.simulate_cancel_confirmed(client_order_id, seq=3)

    resolved = repo.find_by_client_order_id(client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.ACKNOWLEDGED  # unchanged


# -- Cancel race ---------------------------------------------------------------------------


def test_cancel_race_resolves_to_filled_not_cancelled() -> None:
    manager, gateway, repo, _bus = _manager()
    intent = manager.submit(_request())
    client_order_id = intent.client_order_id
    gateway.simulate_ack(client_order_id, "B0001")

    manager.cancel(client_order_id)
    assert gateway.cancelled_order_ids == [client_order_id]

    gateway.simulate_fill(client_order_id, 1, Decimal("18500"), broker_fill_no="F1")

    resolved = repo.find_by_client_order_id(client_order_id)
    assert resolved is not None
    assert resolved.status is OrderStatus.FILLED


def test_cancel_unknown_client_order_id_raises() -> None:
    manager, _gw, _repo_, _bus = _manager()
    with pytest.raises(OrderNotFoundError):
        manager.cancel(ClientOrderId())


# -- Crash-at-submit / idempotent resubmission --------------------------------------------


def test_crash_at_submit_recovers_without_a_second_order() -> None:
    shared_repo = _repo()
    event_bus_1 = FakeEventBus()
    gateway_1 = MockTradeGateway(event_publisher=event_bus_1)
    manager_1, _gw1, _r1, _b1 = _manager(repo=shared_repo, gateway=gateway_1, event_bus=event_bus_1)

    request = _request(idempotency_key="crash-key")
    first_attempt = manager_1.submit(request)
    assert first_attempt.status is OrderStatus.SUBMITTING
    # simulate a crash: no ack/fill ever arrives for gateway_1

    # "restart": a fresh OrderManager over the same durable repository
    event_bus_2 = FakeEventBus()
    gateway_2 = MockTradeGateway(event_publisher=event_bus_2)  # empty broker query results
    manager_2, _gw2, _r2, _b2 = _manager(repo=shared_repo, gateway=gateway_2, event_bus=event_bus_2)

    manager_2.reconcile_on_startup()
    reconciled = shared_repo.find_by_idempotency_key("crash-key")
    assert reconciled is not None
    assert reconciled.status is OrderStatus.UNKNOWN  # no matching broker record found

    second_attempt = manager_2.submit(request)
    assert second_attempt.local_order_id == first_attempt.local_order_id

    assert len(gateway_1.submitted_orders) == 1
    assert len(gateway_2.submitted_orders) == 0  # never resent


def test_crash_at_submit_reconciliation_adopts_broker_known_order() -> None:
    """The broker actually did receive the original submission before the crash —
    reconciliation must adopt its reported status rather than staying UNKNOWN."""
    shared_repo = _repo()
    gateway_1 = MockTradeGateway(event_publisher=FakeEventBus())
    manager_1, _gw1, _r1, _b1 = _manager(repo=shared_repo, gateway=gateway_1)
    request = _request(idempotency_key="crash-key-2")
    first_attempt = manager_1.submit(request)

    broker_report = OrderReport(
        client_order_id=first_attempt.client_order_id,
        status=OrderStatus.ACKNOWLEDGED,
        broker_seq_no=1,
        at=Timestamp.now(),
        broker_order_no="B0001",
    )
    gateway_2 = MockTradeGateway(event_publisher=FakeEventBus())
    gateway_2.set_order_reports([broker_report])
    manager_2, _gw2, _r2, _b2 = _manager(repo=shared_repo, gateway=gateway_2)

    manager_2.reconcile_on_startup()
    reconciled = shared_repo.find_by_idempotency_key("crash-key-2")
    assert reconciled is not None
    assert reconciled.status is OrderStatus.ACKNOWLEDGED
    assert reconciled.broker_order_no == "B0001"


# -- One workflow per contract -------------------------------------------------------------


def test_second_submit_while_active_raises() -> None:
    manager, _gw, _repo_, _bus = _manager()
    manager.submit(_request(idempotency_key="key-1"))
    with pytest.raises(ActiveWorkflowInProgressError):
        manager.submit(_request(idempotency_key="key-2"))


def test_resubmitting_same_idempotency_key_is_deduped_no_second_order() -> None:
    manager, gateway, _repo_, _bus = _manager()
    first = manager.submit(_request(idempotency_key="key-1"))
    second = manager.submit(_request(idempotency_key="key-1"))
    assert first.local_order_id == second.local_order_id
    assert len(gateway.submitted_orders) == 1


# -- Exposure cap ---------------------------------------------------------------------------


def test_exposure_cap_rejects_when_worst_case_would_exceed_max_lots() -> None:
    manager, _gw, _repo_, _bus = _manager(position_lookup=lambda a, i, c: NetPosition(2))
    with pytest.raises(OrderExposureExceededError):
        manager.submit(_request(side=Side.BUY, quantity=1))


def test_exposure_cap_allows_closing_order_that_reduces_exposure() -> None:
    manager, _gw, _repo_, _bus = _manager(
        position_lookup=lambda a, i, c: NetPosition(1), order_timeout_seconds=30.0
    )
    intent = manager.submit(_request(side=Side.SELL, quantity=1, kind=OrderKind.CLOSE))
    assert intent.status is OrderStatus.SUBMITTING
