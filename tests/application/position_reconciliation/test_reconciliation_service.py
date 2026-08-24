from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from tfx_quant.application.events.events import (
    BrokerSessionReady,
    Event,
    FillReceived,
    ManualPositionSyncCompleted,
    PositionDiscrepancyDetected,
    ReversalFlatConfirmed,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.position_reconciliation.errors import (
    ManualSyncBlockedError,
    StaleSyncConfirmationError,
)
from tfx_quant.application.position_reconciliation.reconciliation_service import (
    PositionReconciliationService,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderIntent, OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.position_reconciliation import (
    DiscrepancyKind,
    PositionBaseline,
    ReconciliationTrigger,
)
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.reversal_workflow import (
    ReversalWorkflowId,
    ReversalWorkflowRecord,
    ReversalWorkflowState,
    ReversalWorkflowStateMachine,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository
from tfx_quant.persistence.sqlite_position_baseline_repository import (
    SqlitePositionBaselineRepository,
)
from tfx_quant.persistence.sqlite_reversal_workflow_repository import (
    SqliteReversalWorkflowRepository,
)

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_OTHER_CONTRACT = ContractMonth(year=2026, month=10)
_PRICE = Price(Decimal("18500"))


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


class FakeBarSignalStateStore:
    def __init__(self) -> None:
        self.cleared: list[tuple[Instrument, ContractMonth]] = []

    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        self.cleared.append((instrument, contract))


@dataclass(frozen=True, slots=True)
class _FakeSelection:
    instrument: Instrument
    contract: ContractMonth


_DEFAULT_SELECTION = _FakeSelection(Instrument.MXF, _CONTRACT)
_UNSET: Any = object()
"""Distinguishes "`selection` not passed to `_setup`" (use `_DEFAULT_SELECTION`) from
an explicit `selection=None` (simulate no instrument/contract selected yet)."""


def _position(
    lots: int,
    *,
    contract: ContractMonth = _CONTRACT,
    account: TradingAccount = _ACCOUNT,
    as_of: Timestamp | None = None,
) -> Position:
    return Position(
        account=account,
        instrument=Instrument.MXF,
        contract=contract,
        net=NetPosition(lots),
        average_price=None if lots == 0 else _PRICE,
        as_of=as_of if as_of is not None else Timestamp.now(),
    )


def _order_intent(
    *, contract: ContractMonth = _CONTRACT, status: OrderStatus = OrderStatus.ACKNOWLEDGED
) -> OrderIntent:
    now = Timestamp.now()
    return OrderIntent(
        local_order_id=LocalOrderId(),
        client_order_id=ClientOrderId(),
        workflow_id="wf-1",
        idempotency_key="key-1",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=contract,
        side=Side.BUY,
        kind=OrderKind.OPEN,
        quantity=Quantity(1),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _reversal_record(
    *, state: ReversalWorkflowState, contract: ContractMonth = _CONTRACT
) -> ReversalWorkflowRecord:
    now = Timestamp.now()
    return ReversalWorkflowRecord(
        workflow_id=ReversalWorkflowId(),
        trigger_key=f"trigger-{state.value}",
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=contract,
        state=state,
        created_at=now,
        updated_at=now,
    )


@dataclass
class Harness:
    service: PositionReconciliationService
    gateway: MockTradeGateway
    order_repo: SqliteOrderRepository
    reversal_repo: SqliteReversalWorkflowRepository
    baseline_repo: SqlitePositionBaselineRepository
    order_manager: OrderManager
    bar_signal_state_store: FakeBarSignalStateStore
    strategy_state_machine: StrategyStateMachine
    clock: FakeClock
    event_bus: FakeEventBus
    selection: list[_FakeSelection | None]
    account_holder: list[TradingAccount | None]


def _setup(
    *,
    positions: tuple[Position, ...] = (),
    now: Timestamp | None = None,
    initial_strategy_state: StrategyState = StrategyState.RUNNING,
    selection: _FakeSelection | None = _UNSET,
    account: TradingAccount | None = _ACCOUNT,
) -> Harness:
    event_bus = FakeEventBus()
    gateway = MockTradeGateway(event_publisher=event_bus, positions=list(positions))
    order_repo = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    reversal_repo = SqliteReversalWorkflowRepository(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    baseline_repo = SqlitePositionBaselineRepository(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    bar_signal_state_store = FakeBarSignalStateStore()
    strategy_state_machine = StrategyStateMachine(initial=initial_strategy_state)
    clock = FakeClock(now if now is not None else Timestamp.now())

    selection_holder: list[_FakeSelection | None] = [
        _DEFAULT_SELECTION if selection is _UNSET else selection
    ]
    account_holder: list[TradingAccount | None] = [account]

    service = PositionReconciliationService(
        trade_gateway=gateway,
        order_repository=order_repo,
        reversal_workflow_repository=reversal_repo,
        baseline_repository=baseline_repo,
        bar_signal_state_store=bar_signal_state_store,
        strategy_state_machine=strategy_state_machine,
        clock=clock,
        event_bus=event_bus,
        current_selection=lambda: selection_holder[0],
        selected_account=lambda: account_holder[0],
        poll_interval_seconds=9999,
    )
    order_manager = OrderManager(
        trade_gateway=gateway,
        order_repository=order_repo,
        clock=clock,
        id_generator=UuidIdGenerator(),
        event_bus=event_bus,
        position_lookup=service.expected_net_lookup,
    )
    return Harness(
        service=service,
        gateway=gateway,
        order_repo=order_repo,
        reversal_repo=reversal_repo,
        baseline_repo=baseline_repo,
        order_manager=order_manager,
        bar_signal_state_store=bar_signal_state_store,
        strategy_state_machine=strategy_state_machine,
        clock=clock,
        event_bus=event_bus,
        selection=selection_holder,
        account_holder=account_holder,
    )


# -- reconcile(): matched / skipped ---------------------------------------------------


def test_reconcile_matches_when_expected_and_actual_are_both_flat() -> None:
    h = _setup(positions=())
    record = h.service.reconcile(trigger=ReconciliationTrigger.MANUAL_REQUERY)
    assert record is not None
    assert record.discrepancy is DiscrepancyKind.NONE
    assert record.paused is False
    assert h.strategy_state_machine.state is StrategyState.RUNNING


def test_reconcile_skipped_when_no_instrument_selected() -> None:
    h = _setup(selection=None)
    record = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert record is None


def test_reconcile_skipped_when_no_account_selected() -> None:
    h = _setup(account=None)
    record = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert record is None


# -- reconcile(): discrepancy classification and pause ---------------------------------


@pytest.mark.parametrize(
    "expected_lots,actual_lots,want_kind",
    [
        (0, 1, DiscrepancyKind.DIRECTION),  # 手機 App 建倉
        (1, -1, DiscrepancyKind.DIRECTION),  # 反向持倉
        (2, 1, DiscrepancyKind.QUANTITY),  # 部分平倉
        (1, 0, DiscrepancyKind.DIRECTION),  # 平倉
    ],
)
def test_reconcile_detects_discrepancy_and_pauses_strategy(
    expected_lots: int, actual_lots: int, want_kind: DiscrepancyKind
) -> None:
    h = _setup(positions=(_position(actual_lots),))
    if expected_lots != 0:
        h.baseline_repo.upsert(
            PositionBaseline(
                account=_ACCOUNT,
                instrument=Instrument.MXF,
                contract=_CONTRACT,
                expected_net=NetPosition(expected_lots),
                updated_at=h.clock.now(),
                source="fill",
            )
        )

    record = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)

    assert record is not None
    assert record.discrepancy is want_kind
    assert record.paused is True
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE
    assert len(h.event_bus.published) == 1
    event = h.event_bus.published[0]
    assert isinstance(event, PositionDiscrepancyDetected)
    assert event.discrepancy is want_kind
    assert event.resulting_strategy_state is StrategyState.PAUSED_SAFE


def test_reconcile_when_not_running_reports_current_state_without_escalating() -> None:
    """A discrepancy found while the strategy is already stopped/paused/faulted must
    report that state as-is, never force a further transition (in particular, never
    escalate an already-`PAUSED_SAFE` strategy to `FAULTED` just because the same
    still-unresolved mismatch is detected again on a later trigger)."""
    h = _setup(positions=(_position(1),), initial_strategy_state=StrategyState.STOPPED)
    record = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert record is not None
    assert record.paused is True
    assert record.resulting_strategy_state == StrategyState.STOPPED.value
    assert h.strategy_state_machine.state is StrategyState.STOPPED


def test_repeated_reconcile_of_the_same_unresolved_mismatch_does_not_escalate_to_faulted() -> None:
    h = _setup(positions=(_position(1),))
    first = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    second = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert first is not None and first.resulting_strategy_state == StrategyState.PAUSED_SAFE.value
    assert second is not None and second.resulting_strategy_state == StrategyState.PAUSED_SAFE.value
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE


def test_reconcile_never_auto_corrects_the_baseline_on_mismatch() -> None:
    """ "不得把持倉差異自動解釋成策略成交" — a mismatch must never silently adopt the
    broker's number as the new expected baseline."""
    h = _setup(positions=(_position(1),))
    h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert h.service.expected_net_lookup(_ACCOUNT, Instrument.MXF, _CONTRACT) == NetPosition(0)
    assert h.gateway.submitted_orders == []


def test_reconcile_logs_but_does_not_compare_other_contract_positions() -> None:
    h = _setup(positions=(_position(0, contract=_CONTRACT), _position(1, contract=_OTHER_CONTRACT)))
    record = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert record is not None
    assert record.discrepancy is DiscrepancyKind.NONE
    assert record.other_contract_position_count == 1
    assert record.paused is False


def test_reconcile_query_failure_is_not_treated_as_a_discrepancy() -> None:
    h = _setup()
    h.gateway.fail_next_query_positions(ConnectionError("timeout"))

    record = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)

    assert record is not None
    assert record.query_succeeded is False
    assert record.query_error == "timeout"
    assert record.actual_net is None
    assert record.paused is False
    assert h.strategy_state_machine.state is StrategyState.RUNNING
    assert h.event_bus.published == []


# -- Event-driven triggers --------------------------------------------------------------


def test_fill_updates_baseline_incrementally_and_reconciles() -> None:
    h = _setup(positions=(_position(1),))
    request = OrderRequest(
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=Side.BUY,
        quantity=Quantity(1),
        price=_PRICE,
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
        idempotency_key="k1",
        workflow_id="wf1",
        reason="test",
    )
    intent = h.order_manager.submit(request)
    h.gateway.simulate_ack(intent.client_order_id, "B1")

    h.gateway.simulate_fill(intent.client_order_id, 1, Decimal("18500"), broker_fill_no="F1")

    assert h.service.expected_net_lookup(_ACCOUNT, Instrument.MXF, _CONTRACT) == NetPosition(1)
    discrepancy_events = [
        e for e in h.event_bus.published if isinstance(e, PositionDiscrepancyDetected)
    ]
    assert discrepancy_events == []  # baseline now matches the broker's position(1)


def test_fill_for_unmatched_client_order_id_is_logged_and_ignored() -> None:
    h = _setup()
    fill = Fill(
        client_order_id=ClientOrderId(),
        instrument=Instrument.MXF,
        side=Side.BUY,
        quantity=Quantity(1),
        price=_PRICE,
        at=Timestamp.now(),
        broker_fill_no="F-orphan",
        broker_seq_no=1,
    )
    h.event_bus.publish(FillReceived(at=fill.at, fill=fill))
    assert h.service.expected_net_lookup(_ACCOUNT, Instrument.MXF, _CONTRACT) == NetPosition(0)


def test_first_session_ready_is_login_second_is_reconnect() -> None:
    h = _setup(positions=(_position(1),))

    h.event_bus.publish(BrokerSessionReady(at=Timestamp.now(), account=_ACCOUNT))
    h.event_bus.publish(BrokerSessionReady(at=Timestamp.now(), account=_ACCOUNT))

    events = [e for e in h.event_bus.published if isinstance(e, PositionDiscrepancyDetected)]
    assert [e.trigger for e in events] == [
        ReconciliationTrigger.LOGIN,
        ReconciliationTrigger.RECONNECT,
    ]


def test_reversal_flat_confirmed_triggers_reconciliation_for_its_contract() -> None:
    h = _setup(positions=(_position(1),))
    record = _reversal_record(state=ReversalWorkflowState.FLAT_CONFIRMED)
    h.reversal_repo.save(record)

    h.event_bus.publish(ReversalFlatConfirmed(at=Timestamp.now(), workflow_id=record.workflow_id))

    events = [e for e in h.event_bus.published if isinstance(e, PositionDiscrepancyDetected)]
    assert len(events) == 1
    assert events[0].trigger is ReconciliationTrigger.REVERSAL_FLAT_GATE


def test_on_clock_tick_reconciles_as_timed_poll() -> None:
    h = _setup(positions=(_position(1),))
    h.service.on_clock_tick()
    events = [e for e in h.event_bus.published if isinstance(e, PositionDiscrepancyDetected)]
    assert len(events) == 1
    assert events[0].trigger is ReconciliationTrigger.TIMED_POLL


def test_on_foreground_return_and_on_strategy_start_delegate_to_reconcile() -> None:
    h = _setup(positions=(_position(1),))
    r1 = h.service.on_foreground_return()
    r2 = h.service.on_strategy_start()
    assert r1 is not None and r1.trigger is ReconciliationTrigger.FOREGROUND_RETURN
    assert r2 is not None and r2.trigger is ReconciliationTrigger.STRATEGY_START


# -- Manual sync flow ---------------------------------------------------------------------


def test_manual_sync_full_lifecycle_pauses_syncs_and_stays_paused() -> None:
    """The acceptance scenario: a mismatch pauses the strategy, "重新查詢" shows the
    broker's actual position, "確認同步" updates the baseline, resets K/signal state and
    any paused reversal workflow, sends no order, and leaves the strategy paused —
    requiring the operator to explicitly restart."""
    h = _setup(positions=(_position(2),))
    paused_reversal = _reversal_record(state=ReversalWorkflowState.STARTED)
    h.reversal_repo.save(paused_reversal)
    paused = ReversalWorkflowStateMachine(paused_reversal).mark_paused(
        reason="ambiguous", at=Timestamp.now()
    )
    h.reversal_repo.update(paused)

    mismatch = h.service.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)
    assert mismatch is not None
    assert mismatch.paused is True
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE

    requeried = h.service.request_manual_requery()
    assert requeried is not None
    assert requeried.actual_net == NetPosition(2)
    assert requeried.broker_snapshot_at is not None

    sync_record = h.service.confirm_manual_sync(
        confirmed_actual_net=requeried.actual_net,
        confirmed_query_at=requeried.broker_snapshot_at,
    )

    assert sync_record.baseline_before == NetPosition(0)
    assert sync_record.baseline_after == NetPosition(2)
    assert h.service.expected_net_lookup(_ACCOUNT, Instrument.MXF, _CONTRACT) == NetPosition(2)
    assert (Instrument.MXF, _CONTRACT) in h.bar_signal_state_store.cleared
    assert sync_record.reversal_workflow_reset is True
    reloaded = h.reversal_repo.find_by_workflow_id(paused_reversal.workflow_id)
    assert reloaded is not None
    assert reloaded.state is ReversalWorkflowState.BLOCKED
    assert sync_record.still_paused_safe is True
    assert h.strategy_state_machine.state is StrategyState.PAUSED_SAFE  # never auto-resumed
    assert h.gateway.submitted_orders == []  # 同步本身不送任何單

    completed_events = [
        e for e in h.event_bus.published if isinstance(e, ManualPositionSyncCompleted)
    ]
    assert len(completed_events) == 1
    assert completed_events[0].correlation_id == sync_record.correlation_id


def test_confirm_manual_sync_blocked_by_active_order() -> None:
    h = _setup(positions=(_position(0),))
    h.order_repo.save_intent(_order_intent(status=OrderStatus.ACKNOWLEDGED))

    with pytest.raises(ManualSyncBlockedError):
        h.service.confirm_manual_sync(
            confirmed_actual_net=NetPosition(0), confirmed_query_at=Timestamp.now()
        )
    assert h.gateway.submitted_orders == []


def test_confirm_manual_sync_blocked_by_unknown_order() -> None:
    h = _setup(positions=(_position(0),))
    h.order_repo.save_intent(_order_intent(status=OrderStatus.UNKNOWN))

    with pytest.raises(ManualSyncBlockedError):
        h.service.confirm_manual_sync(
            confirmed_actual_net=NetPosition(0), confirmed_query_at=Timestamp.now()
        )


def test_confirm_manual_sync_rejects_stale_confirmation() -> None:
    h = _setup(positions=(_position(1),))
    requeried = h.service.request_manual_requery()
    assert requeried is not None
    assert requeried.actual_net is not None
    assert requeried.broker_snapshot_at is not None

    h.gateway.set_positions((_position(2),))  # broker position moved again

    with pytest.raises(StaleSyncConfirmationError):
        h.service.confirm_manual_sync(
            confirmed_actual_net=requeried.actual_net,
            confirmed_query_at=requeried.broker_snapshot_at,
        )
    assert h.gateway.submitted_orders == []


def test_confirm_manual_sync_query_failure_raises_blocked_error() -> None:
    h = _setup(positions=(_position(0),))
    h.gateway.fail_next_query_positions(ConnectionError("timeout"))

    with pytest.raises(ManualSyncBlockedError):
        h.service.confirm_manual_sync(
            confirmed_actual_net=NetPosition(0), confirmed_query_at=Timestamp.now()
        )


def test_confirm_manual_sync_leaves_active_non_paused_reversal_workflow_untouched() -> None:
    h = _setup(positions=(_position(0),))
    active_record = _reversal_record(state=ReversalWorkflowState.STARTED)
    h.reversal_repo.save(active_record)

    sync_record = h.service.confirm_manual_sync(
        confirmed_actual_net=NetPosition(0), confirmed_query_at=Timestamp.now()
    )

    assert sync_record.reversal_workflow_reset is False
    reloaded = h.reversal_repo.find_by_workflow_id(active_record.workflow_id)
    assert reloaded is not None
    assert reloaded.state is ReversalWorkflowState.STARTED
