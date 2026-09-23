"""Execute historical replay decisions through the local TEST broker simulator.

The replay engine already assumes an immediate fill after each signal so later bars can
add, stop, or flatten correctly.  This service gives those assumed fills the same durable
order/fill/position path as every other simulated execution.  It is wired only by the
TEST composition root; production never constructs it.
"""

from __future__ import annotations

import hashlib
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
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.signal import SignalKind
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)


class EventBus(Protocol):
    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...


ReplayInstalled = Callable[[HistoricalReplay], None]
SelectedAccount = Callable[[], TradingAccount | None]


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
    ) -> None:
        self._orders = order_repository
        self._manager = order_manager
        self._gateway = gateway
        self._selected_account = selected_account
        self._replay_installed = replay_installed
        self._lock = threading.RLock()
        self._pending: deque[HistoricalTrade] = deque()
        self._queued_keys: set[str] = set()
        self._active_client_order_id: object | None = None
        self._latest_replay = HistoricalReplay((), None, ())
        event_bus.subscribe(FillReceived, self._on_fill)

    def sync(self, replay: HistoricalReplay) -> None:
        """Queue every not-yet-filled replay decision; safe to call on every UI refresh."""
        with self._lock:
            self._latest_replay = replay
            if self._selected_account() is None:
                return
            for candidate in replay.trades:
                key = candidate.decision.intent_key
                if key is None or key in self._queued_keys:
                    continue
                existing = self._orders.find_by_idempotency_key(key)
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

    def _submit_next(self) -> None:
        if self._active_client_order_id is not None:
            return
        account = self._selected_account()
        if account is None:
            return
        while self._pending:
            candidate = self._pending.popleft()
            key = candidate.decision.intent_key
            assert key is not None
            existing = self._orders.find_by_idempotency_key(key)
            if existing is not None:
                if existing.status is OrderStatus.FILLED:
                    self._queued_keys.discard(key)
                    continue
                if existing.is_active:
                    self._active_client_order_id = existing.client_order_id
                    return
                log_warning(
                    _logger,
                    "historical_simulation_terminal_order_not_replayed",
                    intent_key=key,
                    status=existing.status.value,
                )
                self._queued_keys.discard(key)
                continue

            kind = (
                OrderKind.CLOSE
                if candidate.decision.signal_kind is SignalKind.EXIT_ALL
                else OrderKind.OPEN
            )
            request = OrderRequest(
                account=account,
                instrument=candidate.record.bar.instrument,
                contract=candidate.record.bar.contract,
                side=candidate.side,
                quantity=Quantity(candidate.quantity),
                price=Price(candidate.decision.current_price),
                kind=kind,
                time_in_force=TimeInForce.ROD,
                idempotency_key=key,
                workflow_id=key,
                reason=f"歷史模擬成交：{candidate.decision.reason}",
            )
            try:
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

            self._active_client_order_id = intent.client_order_id
            token = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
            self._gateway.simulate_ack(
                intent.client_order_id,
                f"HS-B-{token}",
                at=candidate.decision.at,
            )
            self._gateway.simulate_fill(
                intent.client_order_id,
                candidate.quantity,
                candidate.decision.current_price,
                broker_fill_no=f"HS-F-{token}",
                at=candidate.decision.at,
            )
            log_info(
                _logger,
                "historical_simulation_fill_scheduled",
                intent_key=key,
                side=candidate.side.value,
                quantity=candidate.quantity,
                price=str(candidate.decision.current_price),
            )
            return

        self._replay_installed(self._latest_replay)


__all__ = ["HistoricalSimulationService"]
