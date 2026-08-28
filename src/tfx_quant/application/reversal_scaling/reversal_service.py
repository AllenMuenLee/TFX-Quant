"""ReversalWorkflowService — Feature 07's multi-step, persisted, recoverable reversal
workflow, sitting above `application.order_management.OrderManager` (Feature 06) — the
only way this service ever sends an order to the broker.

Crash-safety is deliberately *not* re-implemented here: every order submission uses a
deterministic, workflow-derived `idempotency_key` (`f"{workflow_id}:close"`/
`f"{workflow_id}:entry"`), so re-running a step after a crash reuses `OrderManager`'s own
idempotency-key dedup guarantee instead of this service needing its own. See
`docs/adr/0009-safe-reversal-and-scaling.md`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BrokerSessionReady,
    Event,
    OrderRequiresManualReview,
    OrderStateTransitioned,
    ReversalCompleted,
    ReversalEntrySubmitted,
    ReversalFlatConfirmed,
    ReversalPausedSafe,
    ReversalWorkflowStarted,
    ReverseEntryBlocked,
)
from tfx_quant.application.order_management.errors import (
    ActiveWorkflowInProgressError,
    OrderExposureExceededError,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.reversal_workflow_repository import (
    ReversalWorkflowRepository,
    ReversalWorkflowSaveOutcome,
)
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.application.reversal_scaling.errors import (
    InvalidSignalKindError,
    ReversalAlreadyActiveError,
)
from tfx_quant.application.reversal_scaling.gates import (
    evaluate_flat_confirmation,
    is_too_close_to_eod,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.reversal_workflow import (
    FlatConfirmationResult,
    ReversalWorkflowId,
    ReversalWorkflowRecord,
    ReversalWorkflowState,
    ReversalWorkflowStateMachine,
)
from tfx_quant.domain.signal import SignalKind, StrategySignal
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_error, log_info, log_warning
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)
_DEFAULT_EOD_MARGIN = timedelta(minutes=10)

MarketDataHealthCheck = Callable[[Instrument, ContractMonth], bool]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as `application.
    order_management.order_manager.EventBus`."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


class ReversalWorkflowService:
    def __init__(
        self,
        *,
        order_manager: OrderManager,
        order_repository: OrderRepository,
        reversal_workflow_repository: ReversalWorkflowRepository,
        trade_gateway: TradeGatewayPort,
        clock: Clock,
        event_bus: EventBus,
        session_healthy: Callable[[], bool],
        market_data_healthy: MarketDataHealthCheck,
        eod_margin: timedelta = _DEFAULT_EOD_MARGIN,
    ) -> None:
        self._order_manager = order_manager
        self._order_repository = order_repository
        self._repo = reversal_workflow_repository
        self._trade_gateway = trade_gateway
        self._clock = clock
        self._event_bus = event_bus
        self._session_healthy = session_healthy
        self._market_data_healthy = market_data_healthy
        self._eod_margin = eod_margin
        self._lock = threading.RLock()
        self._tracked: dict[ClientOrderId, ReversalWorkflowId] = {}
        self._reload_tracked()

        event_bus.subscribe(OrderStateTransitioned, self._on_order_state_transitioned)
        event_bus.subscribe(OrderRequiresManualReview, self._on_order_requires_manual_review)
        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)

    def _reload_tracked(self) -> None:
        """Rebuilds the in-memory client-order-id -> workflow-id map from every
        currently-active, non-paused workflow. `PAUSED_SAFE` workflows are deliberately
        excluded — once paused, this service stops reacting to that workflow's linked
        order events (see module docstring's "late fill after pause" note)."""
        tracked: dict[ClientOrderId, ReversalWorkflowId] = {}
        for record in self._repo.list_active():
            if record.state is ReversalWorkflowState.PAUSED_SAFE:
                continue
            if record.close_client_order_id is not None:
                tracked[record.close_client_order_id] = record.workflow_id
            if record.entry_client_order_id is not None:
                tracked[record.entry_client_order_id] = record.workflow_id
        self._tracked = tracked

    # -- Starting a reversal --------------------------------------------------------

    def start_reversal(
        self, signal: StrategySignal, *, account: TradingAccount, price: Price, trigger_key: str
    ) -> ReversalWorkflowRecord:
        if signal.kind is not SignalKind.REVERSE:
            raise InvalidSignalKindError(
                f"ReversalWorkflowService only handles REVERSE signals, got {signal.kind.value}"
            )
        with self._lock:
            existing = self._repo.find_by_trigger_key(trigger_key)
            if existing is not None:
                log_info(
                    _logger,
                "reversal_submit_deduped",
                audit=True,
                    trigger_key=trigger_key,
                    workflow_id=str(existing.workflow_id.value),
                    state=existing.state.value,
                )
                return existing

            active = self._repo.find_active_for_contract(
                account, signal.instrument, signal.contract
            )
            if active:
                raise ReversalAlreadyActiveError(
                    f"{signal.instrument.value} {signal.contract.code} 已有進行中的反手 workflow"
                )

            now = self._clock.now()
            record = ReversalWorkflowRecord(
                workflow_id=ReversalWorkflowId(),
                trigger_key=trigger_key,
                account=account,
                instrument=signal.instrument,
                contract=signal.contract,
                state=ReversalWorkflowState.STARTED,
                created_at=now,
                updated_at=now,
                price=price,
            )

            if is_too_close_to_eod(now, margin=self._eod_margin):
                outcome = self._repo.save(record)
                if outcome is ReversalWorkflowSaveOutcome.DUPLICATE_KEY:
                    raced = self._repo.find_by_trigger_key(trigger_key)
                    assert raced is not None
                    return raced
                reason = "過於接近 04:55 收盤平倉時間，不啟動反手 workflow"
                blocked = ReversalWorkflowStateMachine(record).mark_blocked(reason=reason, at=now)
                self._repo.update(blocked)
                log_warning(
                    _logger,
                    "reversal_blocked_too_close_to_eod",
                    audit=True,
                    workflow_id=str(blocked.workflow_id.value),
                    trigger_key=trigger_key,
                )
                self._publish(
                    ReverseEntryBlocked(
                        at=now, workflow_id=blocked.workflow_id, reason=reason, result=None
                    )
                )
                return blocked

            outcome = self._repo.save(record)
            if outcome is ReversalWorkflowSaveOutcome.DUPLICATE_KEY:
                raced = self._repo.find_by_trigger_key(trigger_key)
                assert raced is not None
                return raced

            log_info(
                _logger,
                "reversal_workflow_started",
                audit=True,
                workflow_id=str(record.workflow_id.value),
                account_no=mask_account(account.account_no),
                instrument=signal.instrument.value,
                contract=signal.contract.code,
                trigger_key=trigger_key,
            )
            self._publish(
                ReversalWorkflowStarted(
                    at=now,
                    workflow_id=record.workflow_id,
                    account=account,
                    instrument=signal.instrument,
                    contract=signal.contract,
                    trigger_key=trigger_key,
                )
            )
            return self._advance(record)

    # -- The step dispatcher: shared by start_reversal() and resume_pending_workflows() --

    def _advance(self, record: ReversalWorkflowRecord) -> ReversalWorkflowRecord:
        if record.state is ReversalWorkflowState.STARTED:
            return self._advance_from_started(record)
        if record.state is ReversalWorkflowState.POSITION_QUERIED:
            return self._advance_from_position_queried(record)
        if record.state is ReversalWorkflowState.CLOSE_FILLED_BY_REPORT:
            return self._advance_from_close_filled(record)
        if record.state is ReversalWorkflowState.FLAT_CONFIRMED:
            return self._advance_from_flat_confirmed(record)
        return record  # CLOSE_ORDER_SUBMITTED / ENTRY_ORDER_SUBMITTED: event-driven only

    def _advance_from_started(self, record: ReversalWorkflowRecord) -> ReversalWorkflowRecord:
        positions = self._trade_gateway.query_positions()
        matching = _find_position(positions, record.account, record.instrument, record.contract)
        net = matching.net if matching is not None else NetPosition(0)
        now = self._clock.now()
        machine = ReversalWorkflowStateMachine(record)
        if net.lots == 0:
            reason = "已無持倉，無需反手"
            updated = machine.mark_blocked(reason=reason, at=now)
            self._update_and_untrack(updated)
            log_warning(
                _logger, "reversal_blocked_already_flat", audit=True,
                workflow_id=str(updated.workflow_id.value)
            )
            self._publish(
                ReverseEntryBlocked(
                    at=now, workflow_id=updated.workflow_id, reason=reason, result=None
                )
            )
            return updated
        updated = machine.mark_position_queried(starting_net=net, at=now)
        self._repo.update(updated)
        log_info(
            _logger,
            "reversal_position_queried",
            audit=True,
            workflow_id=str(updated.workflow_id.value),
            starting_net=net.lots,
            reversal_side=updated.reversal_side.value if updated.reversal_side else None,
        )
        return self._advance(updated)

    def _advance_from_position_queried(
        self, record: ReversalWorkflowRecord
    ) -> ReversalWorkflowRecord:
        assert record.starting_net is not None
        assert record.reversal_side is not None
        assert record.price is not None
        request = OrderRequest(
            account=record.account,
            instrument=record.instrument,
            contract=record.contract,
            side=record.reversal_side,
            quantity=Quantity(abs(record.starting_net.lots)),
            price=record.price,
            kind=OrderKind.CLOSE,
            time_in_force=TimeInForce.ROD,
            idempotency_key=f"{record.workflow_id.value}:close",
            workflow_id=str(record.workflow_id.value),
            reason=f"reversal workflow {record.workflow_id.value} close leg",
        )
        try:
            order_intent = self._order_manager.submit(request)
        except (ActiveWorkflowInProgressError, OrderExposureExceededError) as exc:
            return self._pause(record, reason=f"平倉委託送出失敗：{exc}")
        now = self._clock.now()
        updated = ReversalWorkflowStateMachine(record).mark_close_submitted(
            client_order_id=order_intent.client_order_id, at=now
        )
        self._repo.update(updated)
        self._tracked[order_intent.client_order_id] = updated.workflow_id
        log_info(
            _logger,
            "reversal_close_order_submitted",
            audit=True,
            workflow_id=str(updated.workflow_id.value),
            client_order_id=str(order_intent.client_order_id.value),
            quantity=request.quantity.lots,
            side=request.side.value,
        )
        return updated

    def _advance_from_close_filled(self, record: ReversalWorkflowRecord) -> ReversalWorkflowRecord:
        now = self._clock.now()
        positions = self._trade_gateway.query_positions()
        matching = _find_position(positions, record.account, record.instrument, record.contract)
        active_orders = self._order_repository.find_active_for_contract(
            record.account, record.instrument, record.contract
        )
        result = evaluate_flat_confirmation(
            position=matching,
            active_orders=active_orders,
            session_healthy=self._session_healthy(),
            market_data_healthy=self._market_data_healthy(record.instrument, record.contract),
        )
        if not result.is_confirmed:
            reason = _describe_flat_failure(result)
            updated = self._pause(record, reason=reason, at=now)
            log_error(
                _logger,
                "reversal_flat_confirmation_failed",
                audit=True,
                workflow_id=str(updated.workflow_id.value),
                is_flat=result.is_flat,
                position_lots=result.position_lots,
                has_active_or_unknown_orders=result.has_active_or_unknown_orders,
                session_healthy=result.session_healthy,
                market_data_healthy=result.market_data_healthy,
            )
            self._publish(
                ReverseEntryBlocked(
                    at=now, workflow_id=updated.workflow_id, reason=reason, result=result
                )
            )
            return updated
        updated = ReversalWorkflowStateMachine(record).mark_flat_confirmed(at=now)
        self._repo.update(updated)
        log_info(
            _logger, "reversal_flat_confirmed", audit=True,
            workflow_id=str(updated.workflow_id.value)
        )
        self._publish(ReversalFlatConfirmed(at=now, workflow_id=updated.workflow_id))
        return self._advance(updated)

    def _advance_from_flat_confirmed(
        self, record: ReversalWorkflowRecord
    ) -> ReversalWorkflowRecord:
        assert record.reversal_side is not None
        assert record.price is not None
        request = OrderRequest(
            account=record.account,
            instrument=record.instrument,
            contract=record.contract,
            side=record.reversal_side,
            quantity=Quantity(1),
            price=record.price,
            kind=OrderKind.OPEN,
            time_in_force=TimeInForce.ROD,
            idempotency_key=f"{record.workflow_id.value}:entry",
            workflow_id=str(record.workflow_id.value),
            reason=f"reversal workflow {record.workflow_id.value} entry leg",
        )
        try:
            order_intent = self._order_manager.submit(request)
        except (ActiveWorkflowInProgressError, OrderExposureExceededError) as exc:
            return self._pause(record, reason=f"反向建倉委託送出失敗：{exc}")
        now = self._clock.now()
        updated = ReversalWorkflowStateMachine(record).mark_entry_submitted(
            client_order_id=order_intent.client_order_id, at=now
        )
        self._repo.update(updated)
        self._tracked[order_intent.client_order_id] = updated.workflow_id
        log_info(
            _logger,
            "reversal_entry_order_submitted",
            audit=True,
            workflow_id=str(updated.workflow_id.value),
            client_order_id=str(order_intent.client_order_id.value),
        )
        self._publish(
            ReversalEntrySubmitted(
                at=now,
                workflow_id=updated.workflow_id,
                entry_client_order_id=order_intent.client_order_id,
            )
        )
        return updated

    def _pause(
        self, record: ReversalWorkflowRecord, *, reason: str, at: Timestamp | None = None
    ) -> ReversalWorkflowRecord:
        now = at if at is not None else self._clock.now()
        updated = ReversalWorkflowStateMachine(record).mark_paused(reason=reason, at=now)
        self._update_and_untrack(updated)
        log_error(
            _logger,
            "reversal_paused_safe",
            audit=True,
            workflow_id=str(updated.workflow_id.value),
            reason=reason,
        )
        self._publish(
            ReversalPausedSafe(
                at=now, workflow_id=updated.workflow_id, state=updated.state, reason=reason
            )
        )
        return updated

    # -- Event handlers ---------------------------------------------------------------

    def _on_order_state_transitioned(self, event: OrderStateTransitioned) -> None:
        with self._lock:
            workflow_id = self._tracked.get(event.client_order_id)
            if workflow_id is None:
                return
            record = self._repo.find_by_workflow_id(workflow_id)
            if record is None or record.state is ReversalWorkflowState.PAUSED_SAFE:
                return
            if event.to_status is OrderStatus.FILLED:
                self._advance_on_fill(record, event.client_order_id, event.at)
            elif event.to_status is OrderStatus.CANCELLED:
                # ReversalWorkflowService never itself cancels a tracked order — an
                # observed cancellation here is always unexpected.
                self._pause(
                    record,
                    reason=f"連結委託 {event.client_order_id.value} 非預期地被取消",
                    at=event.at,
                )

    def _advance_on_fill(
        self, record: ReversalWorkflowRecord, client_order_id: ClientOrderId, at: Timestamp
    ) -> None:
        is_close_leg = (
            record.close_client_order_id == client_order_id
            and record.state is ReversalWorkflowState.CLOSE_ORDER_SUBMITTED
        )
        is_entry_leg = (
            record.entry_client_order_id == client_order_id
            and record.state is ReversalWorkflowState.ENTRY_ORDER_SUBMITTED
        )
        if is_close_leg:
            updated = ReversalWorkflowStateMachine(record).mark_close_filled(at=at)
            self._repo.update(updated)
            log_info(
                _logger, "reversal_close_order_filled", audit=True,
                workflow_id=str(updated.workflow_id.value)
            )
            self._advance(updated)
        elif is_entry_leg:
            updated = ReversalWorkflowStateMachine(record).mark_completed(at=at)
            self._repo.update(updated)
            self._untrack(updated)
            log_info(
                _logger, "reversal_completed", audit=True,
                workflow_id=str(updated.workflow_id.value)
            )
            self._publish(ReversalCompleted(at=at, workflow_id=updated.workflow_id))

    def _on_order_requires_manual_review(self, event: OrderRequiresManualReview) -> None:
        with self._lock:
            workflow_id = self._tracked.get(event.client_order_id)
            if workflow_id is None:
                return
            record = self._repo.find_by_workflow_id(workflow_id)
            if record is None or record.state is ReversalWorkflowState.PAUSED_SAFE:
                return
            reason = (
                f"連結委託 {event.client_order_id.value} 進入 {event.status.value}：{event.reason}"
            )
            self._pause(record, reason=reason, at=event.at)

    def _on_session_ready(self, _event: BrokerSessionReady) -> None:
        self.resume_pending_workflows()

    # -- Restart / reconnect recovery ---------------------------------------------------

    def resume_pending_workflows(self) -> None:
        """Re-derives forward progress for every persisted, non-paused active workflow
        from fresh queries — never blindly re-executing a step from a trusted cache.
        `PAUSED_SAFE` workflows are left exactly as they are; only an operator, not this
        method, can move one forward again (no such path exists in this codebase)."""
        with self._lock:
            self._reload_tracked()
            for record in self._repo.list_active():
                if record.state is ReversalWorkflowState.PAUSED_SAFE:
                    continue
                self._resume_one(record)

    def _resume_one(self, record: ReversalWorkflowRecord) -> None:
        if record.state in (
            ReversalWorkflowState.STARTED,
            ReversalWorkflowState.POSITION_QUERIED,
            ReversalWorkflowState.CLOSE_FILLED_BY_REPORT,
            ReversalWorkflowState.FLAT_CONFIRMED,
        ):
            self._advance(record)
            return
        if record.state is ReversalWorkflowState.CLOSE_ORDER_SUBMITTED:
            assert record.close_client_order_id is not None
            self._resume_linked_order(record, record.close_client_order_id, is_close=True)
            return
        if record.state is ReversalWorkflowState.ENTRY_ORDER_SUBMITTED:
            assert record.entry_client_order_id is not None
            self._resume_linked_order(record, record.entry_client_order_id, is_close=False)
            return

    def _resume_linked_order(
        self, record: ReversalWorkflowRecord, client_order_id: ClientOrderId, *, is_close: bool
    ) -> None:
        now = self._clock.now()
        order = self._order_repository.find_by_client_order_id(client_order_id)
        if order is None:
            # Should never happen — OrderManager persists an intent before any gateway
            # call — but a persistence-layer surprise must never silently resend.
            self._pause(record, reason="重啟後找不到已連結委託", at=now)
            return
        self._tracked[client_order_id] = record.workflow_id
        if order.status is OrderStatus.FILLED:
            if is_close:
                updated = ReversalWorkflowStateMachine(record).mark_close_filled(at=now)
                self._repo.update(updated)
                log_info(
                    _logger,
                    "reversal_close_order_filled",
                    audit=True,
                    workflow_id=str(updated.workflow_id.value),
                    resumed=True,
                )
                self._advance(updated)
            else:
                updated = ReversalWorkflowStateMachine(record).mark_completed(at=now)
                self._repo.update(updated)
                self._untrack(updated)
                log_info(
                    _logger,
                    "reversal_completed",
                    audit=True,
                    workflow_id=str(updated.workflow_id.value),
                    resumed=True,
                )
                self._publish(ReversalCompleted(at=now, workflow_id=updated.workflow_id))
            return
        if order.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN, OrderStatus.CANCELLED):
            reason = f"重啟後查詢連結委託狀態為 {order.status.value}"
            updated = self._pause(record, reason=reason, at=now)
            if is_close:
                self._publish(
                    ReverseEntryBlocked(
                        at=now, workflow_id=updated.workflow_id, reason=reason, result=None
                    )
                )
            return
        # Still genuinely in flight (SUBMITTING/ACKNOWLEDGED/PARTIALLY_FILLED/
        # CANCEL_PENDING) — leave it; OrderManager's own reconciliation (already run
        # earlier at startup) will fire the events that continue driving this workflow.

    # -- Internal helpers ---------------------------------------------------------------

    def _update_and_untrack(self, record: ReversalWorkflowRecord) -> None:
        self._repo.update(record)
        self._untrack(record)

    def _untrack(self, record: ReversalWorkflowRecord) -> None:
        if record.close_client_order_id is not None:
            self._tracked.pop(record.close_client_order_id, None)
        if record.entry_client_order_id is not None:
            self._tracked.pop(record.entry_client_order_id, None)

    def _publish(self, event: Event) -> None:
        self._event_bus.publish(event)


def _find_position(
    positions: Sequence[Position],
    account: TradingAccount,
    instrument: Instrument,
    contract: ContractMonth,
) -> Position | None:
    for position in positions:
        if (
            position.account == account
            and position.instrument == instrument
            and position.contract == contract
        ):
            return position
    return None


def _describe_flat_failure(result: FlatConfirmationResult) -> str:
    reasons: list[str] = []
    if not result.is_flat:
        reasons.append(f"持倉查詢仍非零（{result.position_lots} 口）")
    if result.has_active_or_unknown_orders:
        reasons.append("仍有活動或狀態不明委託")
    if not result.session_healthy:
        reasons.append("session 未就緒")
    if not result.market_data_healthy:
        reasons.append("行情資料不健康")
    return "；".join(reasons) if reasons else "flat 未確認"


__all__ = ["ReversalWorkflowService"]
