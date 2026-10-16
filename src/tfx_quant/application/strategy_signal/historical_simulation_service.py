"""Execute historical replay decisions through the local TEST broker simulator.

The replay engine already assumes an immediate fill after each signal so later bars can
add, stop, or flatten correctly.  This service gives those assumed fills the same durable
order/fill/position path as every other simulated execution.  It is wired only by the
TEST composition root; production never constructs it.

The simulator fills every order inside ``submit_order``, so an order still open from an
earlier run never got its simulated fill: it is executed now when the replay still asks
for it, otherwise cancelled. Each candidate brings the simulated position to the replay's
position after that candidate, so a position already changed by another path (a manual
emergency flatten, for example) is never closed twice or reversed.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from tfx_quant.application.events.events import Event, FillReceived
from tfx_quant.application.order_management.errors import (
    ActiveWorkflowInProgressError,
    OrderExposureExceededError,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.strategy_signal.history_replay import HistoricalReplay, HistoricalTrade
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderIntent, OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_signal_engine import PositionSide
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)


class EventBus(Protocol):
    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...


ReplayInstalled = Callable[[HistoricalReplay], None]
ReplayPending = Callable[[Instrument, ContractMonth], None]
SelectedAccount = Callable[[], TradingAccount | None]


def _signed(side: Side, lots: int) -> int:
    return lots if side is Side.BUY else -lots


def _position_before(candidate: HistoricalTrade) -> int:
    decision = candidate.decision
    if decision.position_side is PositionSide.LONG:
        return decision.position_lots
    if decision.position_side is PositionSide.SHORT:
        return -decision.position_lots
    return 0


class HistoricalSimulationService:
    """Idempotently turn replay candidates into simulated ACKs and fills, in order."""

    def __init__(
        self,
        *,
        order_manager: OrderManager,
        order_repository: OrderRepository,
        gateway: MockTradeGateway,
        event_bus: EventBus,
        selected_account: SelectedAccount,
        replay_installed: ReplayInstalled,
        replay_pending: ReplayPending | None = None,
    ) -> None:
        self._orders = order_repository
        self._manager = order_manager
        self._gateway = gateway
        self._selected_account = selected_account
        self._replay_installed = replay_installed
        self._replay_pending = replay_pending
        self._lock = threading.RLock()
        self._pending: deque[HistoricalTrade] = deque()
        self._queued_keys: set[str] = set()
        self._closed_orphans: set[ClientOrderId] = set()
        self._active_client_order_id: object | None = None
        self._latest_replay = HistoricalReplay((), None, ())
        event_bus.subscribe(FillReceived, self._on_fill)

    def sync(self, replay: HistoricalReplay) -> None:
        """Queue every not-yet-filled replay decision; safe to call on every UI refresh."""
        with self._lock:
            self._latest_replay = replay
            account = self._selected_account()
            if account is None:
                return
            self._cancel_orphans(account, replay)
            for candidate in replay.trades:
                key = candidate.decision.intent_key
                if key is None or key in self._queued_keys:
                    continue
                _, existing = self._execution_order(key)
                if existing is not None and existing.status is OrderStatus.FILLED:
                    continue
                self._pending.append(candidate)
                self._queued_keys.add(key)
            self._submit_next()

    def _on_fill(self, event: FillReceived) -> None:
        with self._lock:
            if event.fill.client_order_id != self._active_client_order_id:
                return
            self._active_client_order_id = None
            self._submit_next()

    def _execution_order(self, key: str) -> tuple[str, OrderIntent | None]:
        """The key a candidate executes under and its order so far. A candidate whose
        order ended without a fill (an earlier run's cancelled orphan) runs again under
        a `#retry` key, since the replay still has it filled."""
        attempt, current = 0, key
        while True:
            existing = self._orders.find_by_idempotency_key(current)
            if existing is None or existing.status is OrderStatus.FILLED or existing.is_active:
                return current, existing
            attempt += 1
            current = f"{key}#retry{attempt}"

    def _cancel_orphans(self, account: TradingAccount, replay: HistoricalReplay) -> None:
        # Only the next candidate due may take over an order an earlier run left open;
        # any other one would block the candidates before it, so it is cancelled.
        due: str | None = None
        for candidate in replay.trades:
            if candidate.decision.intent_key is None:
                continue
            key, existing = self._execution_order(candidate.decision.intent_key)
            if existing is None or existing.status is not OrderStatus.FILLED:
                due = key
                break
        for order in self._orders.list_active():
            if (
                order.account != account
                or order.idempotency_key == due
                or order.client_order_id in self._closed_orphans
                or self._gateway.knows(order.client_order_id)
            ):
                continue
            self._closed_orphans.add(order.client_order_id)
            log_warning(
                _logger,
                "historical_simulation_orphan_order_cancelled",
                intent_key=order.idempotency_key,
                status=order.status.value,
            )
            if order.status is OrderStatus.CREATED:
                self._gateway.simulate_reject(order.client_order_id, "模擬器未曾執行此委託")
            else:
                self._gateway.simulate_cancel_confirmed(order.client_order_id)

    def _net(self, account: TradingAccount, candidate: HistoricalTrade) -> int:
        bar = candidate.record.bar
        return next(
            (
                position.net.lots
                for position in self._gateway.query_positions()
                if position.account == account
                and position.instrument is bar.instrument
                and position.contract == bar.contract
            ),
            0,
        )

    def _suspend_live_engine(self, candidate: HistoricalTrade) -> None:
        # The live engine's state predates these fills; it is replaced by the replay's
        # engine once the queue drains, and must not apply them in between.
        if self._replay_pending is not None:
            self._replay_pending(candidate.record.bar.instrument, candidate.record.bar.contract)

    def _await_fill(self, client_order_id: ClientOrderId) -> bool:
        """Whether to keep going: a fill delivered synchronously is already applied."""
        current = self._orders.find_by_client_order_id(client_order_id)
        if current is not None and current.status is OrderStatus.FILLED:
            return True
        self._active_client_order_id = client_order_id
        return False

    def _execute_orphan(self, existing: OrderIntent, candidate: HistoricalTrade) -> bool:
        assert candidate.decision.current_price is not None
        log_warning(
            _logger,
            "historical_simulation_orphan_order_executed",
            intent_key=existing.idempotency_key,
            status=existing.status.value,
            price=str(candidate.decision.current_price),
        )
        self._suspend_live_engine(candidate)
        order = Order(
            client_order_id=existing.client_order_id,
            account=existing.account,
            instrument=existing.instrument,
            contract=existing.contract,
            side=existing.side,
            quantity=Quantity(existing.remaining_quantity),
            price=Price(candidate.decision.current_price),
            kind=existing.kind,
            time_in_force=TimeInForce.ROD,
        )
        with self._gateway.executing_at(candidate.decision.at):
            self._gateway.execute_existing(order, existing.client_order_id)
        return self._await_fill(existing.client_order_id)

    def _submit_next(self) -> None:
        if self._active_client_order_id is not None:
            return
        account = self._selected_account()
        if account is None:
            return
        while self._pending:
            candidate = self._pending.popleft()
            intent_key = candidate.decision.intent_key
            assert intent_key is not None and candidate.decision.current_price is not None
            key, existing = self._execution_order(intent_key)
            if existing is not None:
                if existing.status is OrderStatus.FILLED:
                    self._queued_keys.discard(intent_key)
                    continue
                if self._gateway.knows(existing.client_order_id):
                    self._active_client_order_id = existing.client_order_id
                    return
                if self._execute_orphan(existing, candidate):
                    continue
                return

            actual = self._net(account, candidate)
            before = _position_before(candidate)
            target = before + _signed(candidate.side, candidate.quantity)
            delta = target - actual
            if actual != before:
                log_warning(
                    _logger,
                    "historical_simulation_position_differs",
                    intent_key=key,
                    simulated_net=actual,
                    replay_net_before=before,
                    replay_net_after=target,
                )
            if delta == 0:
                self._queued_keys.discard(intent_key)
                continue
            side = Side.BUY if delta > 0 else Side.SELL
            if actual != 0 and (actual > 0) != (delta > 0):
                # Never let one order both close and reverse: close only what is held.
                kind, lots = OrderKind.CLOSE, min(abs(delta), abs(actual))
            else:
                kind, lots = OrderKind.OPEN, abs(delta)
            request = OrderRequest(
                account=account,
                instrument=candidate.record.bar.instrument,
                contract=candidate.record.bar.contract,
                side=side,
                quantity=Quantity(lots),
                price=Price(candidate.decision.current_price),
                kind=kind,
                time_in_force=TimeInForce.ROD,
                idempotency_key=key,
                workflow_id=key,
                reason=f"歷史模擬成交：{candidate.decision.reason}",
            )
            self._suspend_live_engine(candidate)
            try:
                with self._gateway.executing_at(candidate.decision.at):
                    intent = self._manager.submit(request)
            except (ActiveWorkflowInProgressError, OrderExposureExceededError) as exc:
                # Another workflow owns the contract. Keep the candidate for the next
                # idempotent sync call rather than reordering historical executions.
                self._pending.appendleft(candidate)
                log_warning(
                    _logger,
                    "historical_simulation_waiting_for_order_gate",
                    intent_key=key,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return

            log_info(
                _logger,
                "historical_simulation_order_executed",
                intent_key=key,
                side=side.value,
                quantity=lots,
                price=str(candidate.decision.current_price),
            )
            if not self._await_fill(intent.client_order_id):
                return

        self._replay_installed(self._latest_replay)


__all__ = ["HistoricalSimulationService"]
