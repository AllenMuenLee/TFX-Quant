"""OrderManager — the only component allowed to turn a trading intent into a real Yuanta
order (implementation prompt: "實作唯一有權將交易意圖轉成元大委託的 order manager").

Broker order/fill reports are the only source of truth: `submit()` never infers success
from `TradeGatewayPort.submit_order()` returning without raising, and every state
transition happens through `domain.order_state_machine.OrderStateMachine`, driven only by
an `OrderReportReceived`/`FillReceived` event or an explicit reconciliation query.

Idempotency is two-layered: `OrderRequest.idempotency_key` is the caller-stable key
`submit()` deduplicates on (a resend of "the same logical decision" — a retry, a repeated
K-bar-driven signal — returns the existing intent and never calls the gateway a second
time); `ClientOrderId`/`LocalOrderId` are minted fresh by this manager itself, never
supplied by the caller, specifically so a caller cannot defeat the dedup guard by
constructing its own `Order`/`ClientOrderId`.

Unlike `MarketDataBarService`'s bar-write path (async, bounded queue, because tick volume
would otherwise block market-data processing — see ADR 0007 decision 5), every
persistence write here is synchronous, on whichever thread called in: order volume is low
and the whole point of this component is a *stronger* durability guarantee ("送單前原子地
持久化"), not throughput.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BrokerSessionReady,
    Event,
    FillReceived,
    OrderReportReceived,
    OrderRequiresManualReview,
    OrderStateTransitioned,
)
from tfx_quant.application.order_management.errors import (
    ActiveWorkflowInProgressError,
    OrderExposureExceededError,
    OrderNotFoundError,
    UnsupportedTradeInstrumentError,
)
from tfx_quant.application.order_management.validation import validate_exposure_within_cap
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.identity import IdGenerator
from tfx_quant.application.ports.order_repository import OrderIntentSaveOutcome, OrderRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import (
    LocalOrderId,
    OrderIntent,
    OrderReport,
    OrderStateMachine,
    OrderStatus,
)
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_error, log_info, log_warning
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)

_DEFAULT_ORDER_TIMEOUT_SECONDS = 30.0
_DEFAULT_CLOCK_INTERVAL_SECONDS = 5.0
_TIMER_STOP = object()

PositionLookup = Callable[[TradingAccount, Instrument, ContractMonth], NetPosition]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as
    `application.market_data.bar_service.EventBus`."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


ApplyFn = Callable[[OrderStateMachine], "tuple[OrderIntent, bool]"]


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Input DTO for `OrderManager.submit` — same role as `application.ports.
    broker_session.LoginRequest`. The caller never builds `Order`/`ClientOrderId`
    itself; `OrderManager` mints both only once, on first sight of `idempotency_key`."""

    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    side: Side
    quantity: Quantity
    price: Price
    kind: OrderKind
    time_in_force: TimeInForce
    idempotency_key: str
    workflow_id: str
    reason: str


class OrderManager:
    def __init__(
        self,
        *,
        trade_gateway: TradeGatewayPort,
        order_repository: OrderRepository,
        clock: Clock,
        id_generator: IdGenerator,
        event_bus: EventBus,
        position_lookup: PositionLookup,
        order_timeout_seconds: float = _DEFAULT_ORDER_TIMEOUT_SECONDS,
        clock_interval_seconds: float = _DEFAULT_CLOCK_INTERVAL_SECONDS,
    ) -> None:
        self._trade_gateway = trade_gateway
        self._order_repository = order_repository
        self._clock = clock
        self._id_generator = id_generator
        self._event_bus = event_bus
        self._position_lookup = position_lookup
        self._order_timeout_seconds = order_timeout_seconds
        self._clock_interval_seconds = clock_interval_seconds
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._running = False

        event_bus.subscribe(OrderReportReceived, self._on_order_report)
        event_bus.subscribe(FillReceived, self._on_fill)
        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)

    # -- Lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._schedule_next_tick()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_next_tick(self) -> None:
        timer = threading.Timer(self._clock_interval_seconds, self._on_timer_fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer_fire(self) -> None:
        self.on_clock_tick()
        with self._lock:
            if self._running:
                self._schedule_next_tick()

    # -- Submit / cancel ----------------------------------------------------------------

    def submit(self, request: OrderRequest) -> OrderIntent:
        with self._lock:
            if request.instrument is not Instrument.MXF:
                log_warning(
                    _logger,
                    "order_submit_rejected_non_mxf",
                    requested_instrument=request.instrument.value,
                    contract=request.contract.code,
                )
                raise UnsupportedTradeInstrumentError("交易商品固定為小台指（MXF）")
            existing = self._order_repository.find_by_idempotency_key(request.idempotency_key)
            if existing is not None:
                log_info(
                    _logger,
                    "order_submit_deduped",
                    idempotency_key=request.idempotency_key,
                    local_order_id=str(existing.local_order_id.value),
                    status=existing.status.value,
                )
                return existing

            active = self._order_repository.find_active_for_contract(
                request.account, request.instrument, request.contract
            )
            if active:
                raise ActiveWorkflowInProgressError(
                    f"{request.instrument.value} {request.contract.code} 已有進行中的委託"
                    f"（workflow_id={active[0].workflow_id}），禁止送出新委託"
                )

            current_position = self._position_lookup(
                request.account, request.instrument, request.contract
            )
            reason = validate_exposure_within_cap(
                current_position,
                active,
                candidate_side=request.side,
                candidate_lots=request.quantity.lots,
            )
            if reason is not None:
                raise OrderExposureExceededError(reason)

            now = self._clock.now()
            client_order_id = self._id_generator.new_client_order_id()
            intent = OrderIntent(
                local_order_id=LocalOrderId(),
                client_order_id=client_order_id,
                workflow_id=request.workflow_id,
                idempotency_key=request.idempotency_key,
                account=request.account,
                instrument=request.instrument,
                contract=request.contract,
                side=request.side,
                kind=request.kind,
                quantity=request.quantity,
                status=OrderStatus.CREATED,
                created_at=now,
                updated_at=now,
            )
            outcome = self._order_repository.save_intent(intent)
            log_info(
                _logger,
                "order_intent_persist_result",
                audit=True,
                local_order_id=str(intent.local_order_id.value),
                workflow_id=intent.workflow_id,
                idempotency_key=intent.idempotency_key,
                instrument=intent.instrument.value,
                contract=intent.contract.code,
                side=intent.side.value,
                kind=intent.kind.value,
                quantity=intent.quantity.lots,
                reason=request.reason,
                outcome=outcome.value,
            )
            if outcome is OrderIntentSaveOutcome.DUPLICATE_KEY:
                raced_existing = self._order_repository.find_by_idempotency_key(
                    request.idempotency_key
                )
                assert raced_existing is not None
                return raced_existing

            machine = OrderStateMachine(intent)
            submitting = machine.mark_submitting(at=self._clock.now())
            stage = getattr(self._order_repository, "stage_submission", None)
            if callable(stage):
                stage(submitting)
            else:
                self._order_repository.update_intent(submitting)
            log_info(
                _logger,
                "order_state_transitioned",
                audit=True,
                workflow_id=submitting.workflow_id,
                trigger="submit",
                local_order_id=str(submitting.local_order_id.value),
                client_order_id=str(submitting.client_order_id.value),
                from_status=OrderStatus.CREATED.value,
                to_status=submitting.status.value,
            )

            order = Order(
                client_order_id=client_order_id,
                account=request.account,
                instrument=request.instrument,
                contract=request.contract,
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                kind=request.kind,
                time_in_force=request.time_in_force,
            )
            try:
                checkpoint = getattr(self._order_repository, "mark_outbox_checkpoint", None)
                if callable(checkpoint):
                    checkpoint(submitting.local_order_id, "BROKER_CALL_STARTED")
                self._trade_gateway.submit_order(order, client_order_id=client_order_id)
                if callable(checkpoint):
                    checkpoint(submitting.local_order_id, "BROKER_CALL_RETURNED")
            except Exception as exc:  # noqa: BLE001 - never let an ambiguous send-result propagate
                if callable(checkpoint):
                    checkpoint(submitting.local_order_id, "BROKER_CALL_FAILED")
                unknown = OrderStateMachine(submitting).mark_unknown(
                    at=self._clock.now(), reason=f"trade_gateway.submit_order raised: {exc}"
                )
                self._order_repository.update_intent(unknown)
                log_error(
                    _logger,
                    "order_submit_gateway_call_failed",
                    audit=True,
                    workflow_id=unknown.workflow_id,
                    local_order_id=str(unknown.local_order_id.value),
                    client_order_id=str(unknown.client_order_id.value),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                self._publish(
                    OrderRequiresManualReview(
                        at=self._clock.now(),
                        local_order_id=unknown.local_order_id,
                        client_order_id=unknown.client_order_id,
                        status=OrderStatus.UNKNOWN,
                        reason="gateway call failed after intent was persisted",
                    )
                )
                return unknown
            return submitting

    def cancel(self, client_order_id: ClientOrderId) -> OrderIntent:
        with self._lock:
            record = self._order_repository.find_by_client_order_id(client_order_id)
            if record is None:
                raise OrderNotFoundError(f"找不到 client_order_id={client_order_id.value} 的委託")
            if record.status is OrderStatus.CANCEL_PENDING or not record.is_active:
                log_info(
                    _logger,
                    "order_cancel_noop",
                    local_order_id=str(record.local_order_id.value),
                    status=record.status.value,
                )
                return record
            machine = OrderStateMachine(record)
            updated = machine.mark_cancel_pending(at=self._clock.now())
            self._order_repository.update_intent(updated)
            log_info(
                _logger,
                "order_state_transitioned",
                audit=True,
                workflow_id=updated.workflow_id,
                trigger="cancel_requested",
                local_order_id=str(updated.local_order_id.value),
                client_order_id=str(updated.client_order_id.value),
                from_status=record.status.value,
                to_status=updated.status.value,
            )
            self._trade_gateway.cancel_order(client_order_id)
            return updated

    # -- Event handlers -----------------------------------------------------------------

    def _on_order_report(self, event: OrderReportReceived) -> None:
        report = event.report
        with self._lock:
            self._handle_incoming(
                client_order_id=report.client_order_id,
                apply=lambda m: m.apply_order_report(report),
                broker_seq_no=report.broker_seq_no,
                kind="order_report",
                now=event.at,
            )

    def _on_fill(self, event: FillReceived) -> None:
        fill = event.fill
        with self._lock:
            self._handle_incoming(
                client_order_id=fill.client_order_id,
                apply=lambda m: m.apply_fill(fill, broker_seq_no=fill.broker_seq_no),
                broker_seq_no=fill.broker_seq_no,
                kind="fill",
                now=event.at,
                broker_fill_no=fill.broker_fill_no,
            )

    def _on_session_ready(self, _event: BrokerSessionReady) -> None:
        self.reconcile_on_startup()

    def _handle_incoming(
        self,
        *,
        client_order_id: ClientOrderId,
        apply: ApplyFn,
        broker_seq_no: int,
        kind: str,
        now: Timestamp,
        **extra_log_fields: Any,
    ) -> None:
        record = self._order_repository.find_by_client_order_id(client_order_id)
        if record is None:
            log_error(
                _logger,
                "order_event_unmatched",
                kind=kind,
                client_order_id=str(client_order_id.value),
                broker_seq_no=broker_seq_no,
                **extra_log_fields,
            )
            return
        from_status = record.status
        machine = OrderStateMachine(record)
        try:
            updated, applied = apply(machine)
        except IllegalStateTransitionError as exc:
            unknown = OrderStateMachine(record).mark_unknown(
                at=now, reason=f"illegal transition applying {kind}: {exc}"
            )
            self._order_repository.update_intent(unknown)
            log_error(
                _logger,
                "order_transition_illegal",
                audit=True,
                workflow_id=record.workflow_id,
                kind=kind,
                local_order_id=str(record.local_order_id.value),
                client_order_id=str(client_order_id.value),
                from_status=from_status.value,
                broker_order_no=_mask_broker_order_no(record.broker_order_no),
                broker_seq_no=broker_seq_no,
                last_applied_broker_seq_no=record.last_applied_broker_seq_no,
                error=str(exc),
                **extra_log_fields,
            )
            self._publish(
                OrderRequiresManualReview(
                    at=now,
                    local_order_id=unknown.local_order_id,
                    client_order_id=client_order_id,
                    status=OrderStatus.UNKNOWN,
                    reason=str(exc),
                )
            )
            return
        if not applied:
            log_info(
                _logger,
                "order_event_duplicate_or_out_of_order",
                audit=True,
                workflow_id=record.workflow_id,
                kind=kind,
                local_order_id=str(record.local_order_id.value),
                client_order_id=str(client_order_id.value),
                from_status=from_status.value,
                last_applied_broker_seq_no=record.last_applied_broker_seq_no,
                incoming_broker_seq_no=broker_seq_no,
                **extra_log_fields,
            )
            return
        self._order_repository.update_intent(updated)
        log_info(
            _logger,
            "order_state_transitioned",
            audit=True,
            workflow_id=updated.workflow_id,
            trigger=kind,
            local_order_id=str(updated.local_order_id.value),
            client_order_id=str(updated.client_order_id.value),
            from_status=from_status.value,
            to_status=updated.status.value,
            broker_order_no=_mask_broker_order_no(updated.broker_order_no),
            cumulative_filled=updated.filled_quantity,
            remaining=updated.remaining_quantity,
            broker_seq_no=broker_seq_no,
            **extra_log_fields,
        )
        self._publish(
            OrderStateTransitioned(
                at=now,
                local_order_id=updated.local_order_id,
                client_order_id=client_order_id,
                from_status=from_status,
                to_status=updated.status,
                trigger=kind,
                broker_order_no=updated.broker_order_no,
            )
        )
        if updated.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN):
            self._publish(
                OrderRequiresManualReview(
                    at=now,
                    local_order_id=updated.local_order_id,
                    client_order_id=client_order_id,
                    status=updated.status,
                    reason=updated.last_event_summary,
                )
            )

    # -- Timeout sweep --------------------------------------------------------------------

    def on_clock_tick(self, now: Timestamp | None = None) -> None:
        """Public so tests can drive the timeout sweep directly, without a real timer —
        same convention as `MarketDataBarService.on_clock_tick`."""
        resolved_now = now if now is not None else self._clock.now()
        with self._lock:
            for record in self._order_repository.list_active():
                age = (resolved_now.value - record.updated_at.value).total_seconds()
                if age <= self._order_timeout_seconds:
                    continue
                machine = OrderStateMachine(record)
                updated = machine.mark_unknown(
                    at=resolved_now, reason="unacknowledged/unfilled timeout"
                )
                self._order_repository.update_intent(updated)
                log_warning(
                    _logger,
                    "order_timeout_marked_unknown",
                    audit=True,
                    workflow_id=updated.workflow_id,
                    local_order_id=str(updated.local_order_id.value),
                    client_order_id=str(updated.client_order_id.value),
                    previous_status=record.status.value,
                    age_seconds=age,
                    timeout_seconds=self._order_timeout_seconds,
                )
                self._publish(
                    OrderRequiresManualReview(
                        at=resolved_now,
                        local_order_id=updated.local_order_id,
                        client_order_id=updated.client_order_id,
                        status=OrderStatus.UNKNOWN,
                        reason="unacknowledged/unfilled timeout",
                    )
                )
                self._follow_up_query_and_log(updated, resolved_now)

    def _follow_up_query_and_log(self, record: OrderIntent, now: Timestamp) -> None:
        reports = self._trade_gateway.query_order_reports()
        fills = self._trade_gateway.query_fills()
        resolved = self._reconcile_one(record, reports, fills, now=now)
        log_info(
            _logger,
            "order_timeout_follow_up_decision",
            audit=True,
            workflow_id=record.workflow_id,
            local_order_id=str(record.local_order_id.value),
            client_order_id=str(record.client_order_id.value),
            resolved_by_query=resolved,
            resent=False,
        )

    # -- Startup / reconnect reconciliation -------------------------------------------------

    def reconcile_on_startup(self) -> None:
        with self._lock:
            reports = self._trade_gateway.query_order_reports()
            fills = self._trade_gateway.query_fills()
            positions = self._trade_gateway.query_positions()
            active = self._order_repository.list_active()
            log_info(
                _logger,
                "order_reconciliation_started",
                active_count=len(active),
                broker_report_count=len(reports),
                broker_fill_count=len(fills),
                broker_position_count=len(positions),
            )
            now = self._clock.now()
            for record in active:
                self._reconcile_one(record, reports, fills, now=now)

    def _reconcile_one(
        self,
        record: OrderIntent,
        reports: Sequence[OrderReport],
        fills: Sequence[Fill],
        *,
        now: Timestamp,
    ) -> bool:
        machine = OrderStateMachine(record)
        matching_reports = sorted(
            (r for r in reports if r.client_order_id == record.client_order_id),
            key=lambda r: r.broker_seq_no,
        )
        matching_fills = sorted(
            (f for f in fills if f.client_order_id == record.client_order_id),
            key=lambda f: f.broker_seq_no,
        )
        applied_any = False
        for report in matching_reports:
            try:
                _, applied = machine.apply_order_report(report)
            except IllegalStateTransitionError:
                continue
            applied_any = applied_any or applied
        for fill in matching_fills:
            _, applied = machine.apply_fill(fill, broker_seq_no=fill.broker_seq_no)
            applied_any = applied_any or applied
        if not applied_any:
            machine.mark_unknown(at=now, reason="no matching broker record found on reconciliation")
        self._order_repository.update_intent(machine.record)
        log_info(
            _logger,
            "order_reconciliation_result",
            audit=True,
            workflow_id=record.workflow_id,
            local_order_id=str(record.local_order_id.value),
            client_order_id=str(record.client_order_id.value),
            matched_report_count=len(matching_reports),
            matched_fill_count=len(matching_fills),
            resolved_status=machine.record.status.value,
        )
        return applied_any

    def _publish(self, event: Event) -> None:
        self._event_bus.publish(event)


def _mask_broker_order_no(broker_order_no: str | None) -> str | None:
    return None if broker_order_no is None else mask_account(broker_order_no)


__all__ = ["OrderManager", "OrderRequest"]
