"""RiskSupervisor — Feature 10's independent, highest-priority risk supervisor.

Two independent responsibilities, deliberately never delegated to `domain.
strategy_signal_engine.StrategySignalEngine`'s own internal state so a bug in the
strategy engine's own MA/candle logic can never bypass either of them:

1. `validate_entry_window()` — the pure, strategy-agnostic 04:55-10:45 no-new-position
   gate (`application.risk.gates.validate_entry_window`) that any caller submitting an
   `OPEN`-kind order (new entry or add-on) must consult before ever calling
   `OrderManager.submit()`. Closing/risk-driven orders are never subject to this gate.
2. The persisted, recoverable "close everything now" workflow behind both the mandatory
   04:55 forced flatten (edge-triggered once per trading day, per contract, on the
   configured clock crossing into the no-entry window) and the operator-confirmed
   emergency-flatten control. Always queries the actual broker position and active
   orders fresh before ever submitting a close order sized to that actual position —
   never a blind, guessed-quantity close — and never reports completion until a fresh
   broker position query independently confirms exactly zero.

**Never auto-flattens a position discovered already open at startup while inside the
no-entry band** — see `_check_startup_safety`'s docstring and the implementation
prompt's explicit "若程式在 04:55 之後啟動且有持倉，保持安全暫停並提示人工執行緊急平倉".
Only the mandatory recurring 04:55 *edge* (observed while this process has been running
continuously since before the band started) auto-triggers a flatten; every other case
requires the operator's emergency-flatten control.

Order submission always goes through `application.order_management.OrderManager`, never
directly through `TradeGatewayPort` — same architecture as `ReversalWorkflowService`/
`ScalingService`. The close order's price is always the most recently closed bar's close
(this supervisor never reads the live Yuanta quote feed for pricing, even though one
exists elsewhere in this codebase for display/staleness purposes — see `docs/adr/0006`);
if no bar has closed yet since this process started, the workflow safely pauses rather
than guessing a price.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import time
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from tfx_quant.application.events.events import (
    BarClosed,
    BrokerSessionReady,
    EodFlattenCompleted,
    EodFlattenPausedSafe,
    EodFlattenWorkflowStarted,
    Event,
    OrderRequiresManualReview,
    OrderStateTransitioned,
    StartupPositionSafetyPauseTriggered,
)
from tfx_quant.application.order_management.errors import (
    ActiveWorkflowInProgressError,
    OrderExposureExceededError,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.eod_flatten_workflow_repository import (
    EodFlattenWorkflowRepository,
    EodFlattenWorkflowSaveOutcome,
)
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.application.reversal_scaling.gates import evaluate_flat_confirmation
from tfx_quant.application.risk import gates
from tfx_quant.application.risk.errors import (
    EodFlattenAlreadyActiveError,
    StaleEmergencyConfirmationError,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.reversal_workflow import FlatConfirmationResult
from tfx_quant.domain.risk import (
    ENTRY_GATE_LOCAL_TIME,
    EOD_FLATTEN_LOCAL_TIME,
    EodFlattenTrigger,
    EodFlattenWorkflowId,
    EodFlattenWorkflowRecord,
    EodFlattenWorkflowState,
    EodFlattenWorkflowStateMachine,
    is_within_no_entry_window,
)
from tfx_quant.domain.strategy_state import StrategyStateMachine, attempt_safe_pause
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_error, log_info, log_warning
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)
_DEFAULT_CLOCK_INTERVAL_SECONDS = 5.0

_EngineKey = tuple[Instrument, ContractMonth]


class Selection(Protocol):
    """Structural stand-in for `application.instrument_selection.selection.
    ResolvedSelection` — same seam as `PositionReconciliationService.Selection`."""

    @property
    def instrument(self) -> Instrument: ...

    @property
    def contract(self) -> ContractMonth: ...


CurrentSelection = Callable[[], "Selection | None"]
SelectedAccount = Callable[[], "TradingAccount | None"]
MarketDataHealthCheck = Callable[[Instrument, ContractMonth], bool]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as `application.
    order_management.order_manager.EventBus`."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


class RiskSupervisor:
    def __init__(
        self,
        *,
        order_manager: OrderManager,
        order_repository: OrderRepository,
        eod_flatten_workflow_repository: EodFlattenWorkflowRepository,
        trade_gateway: TradeGatewayPort,
        clock: Clock,
        event_bus: EventBus,
        strategy_state_machine: StrategyStateMachine,
        session_healthy: Callable[[], bool],
        market_data_healthy: MarketDataHealthCheck,
        current_selection: CurrentSelection,
        selected_account: SelectedAccount,
        eod_flatten_local_time: time = EOD_FLATTEN_LOCAL_TIME,
        entry_gate_local_time: time = ENTRY_GATE_LOCAL_TIME,
        clock_interval_seconds: float = _DEFAULT_CLOCK_INTERVAL_SECONDS,
    ) -> None:
        self._order_manager = order_manager
        self._order_repository = order_repository
        self._repo = eod_flatten_workflow_repository
        self._trade_gateway = trade_gateway
        self._clock = clock
        self._event_bus = event_bus
        self._strategy_state_machine = strategy_state_machine
        self._session_healthy = session_healthy
        self._market_data_healthy = market_data_healthy
        self._current_selection = current_selection
        self._selected_account = selected_account
        self._eod_flatten_local_time = eod_flatten_local_time
        self._entry_gate_local_time = entry_gate_local_time
        self._clock_interval_seconds = clock_interval_seconds
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._running = False
        self._tracked: dict[ClientOrderId, EodFlattenWorkflowId] = {}
        self._last_in_band: dict[_EngineKey, bool] = {}
        self._last_price: dict[_EngineKey, Decimal] = {}
        self._startup_safety_checked = False

        event_bus.subscribe(BarClosed, self._on_bar_closed)
        event_bus.subscribe(OrderStateTransitioned, self._on_order_state_transitioned)
        event_bus.subscribe(OrderRequiresManualReview, self._on_order_requires_manual_review)
        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)

    # -- Lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._reload_tracked()
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

    def on_clock_tick(self, now: Timestamp | None = None) -> None:
        """Public so tests can drive both the scheduled-flatten trigger and the pending-
        workflow poll directly, without a real timer — same convention as `OrderManager.
        on_clock_tick`."""
        resolved_now = now if now is not None else self._clock.now()
        with self._lock:
            for record in self._repo.list_active():
                if record.state is EodFlattenWorkflowState.PAUSED_SAFE:
                    continue
                self._resume_one(record)
            self._maybe_trigger_scheduled_flatten(resolved_now)

    # -- The independent entry-window gate -----------------------------------------------

    def validate_entry_window(self, now: Timestamp | None = None) -> str | None:
        """Callers submitting an `OPEN`-kind order (new entry or add-on) must consult
        this before ever calling `OrderManager.submit()` — see the module docstring."""
        resolved_now = now if now is not None else self._clock.now()
        return gates.validate_entry_window(
            resolved_now,
            eod_flatten_local_time=self._eod_flatten_local_time,
            entry_gate_local_time=self._entry_gate_local_time,
        )

    # -- Scheduled (04:55) trigger --------------------------------------------------------

    def _maybe_trigger_scheduled_flatten(self, now: Timestamp) -> None:
        selection = self._current_selection()
        account = self._selected_account()
        if selection is None or account is None:
            return
        key = (selection.instrument, selection.contract)
        in_band = is_within_no_entry_window(
            now,
            eod_flatten_local_time=self._eod_flatten_local_time,
            entry_gate_local_time=self._entry_gate_local_time,
        )
        previous_in_band = self._last_in_band.get(key)
        self._last_in_band[key] = in_band
        if not in_band:
            return
        if previous_in_band is not False:
            # Either this is the first observation of this key this run (`None` — the
            # process (re)started already inside the band, and never auto-triggers for
            # that; `_check_startup_safety` on the first `BrokerSessionReady` owns
            # pausing and alerting for that case instead — see the module docstring),
            # or the band was already open on the previous tick too (`True` — not a
            # fresh crossing, and the trigger-key dedup below already covers today).
            if previous_in_band is None:
                log_info(
                    _logger,
                    "eod_flatten_scheduled_trigger_skipped_startup_inside_band",
                    instrument=selection.instrument.value,
                    contract=selection.contract.code,
                    now=now.value.isoformat(),
                )
            return
        trigger_key = (
            f"scheduled:{selection.instrument.value}:{selection.contract.code}:"
            f"{now.value.date().isoformat()}"
        )
        if self._repo.find_by_trigger_key(trigger_key) is not None:
            return
        if not self._session_healthy():
            log_warning(
                _logger,
                "eod_flatten_scheduled_trigger_deferred_session_unhealthy",
                trigger_key=trigger_key,
            )
            return
        self._begin(
            trigger=EodFlattenTrigger.SCHEDULED,
            account=account,
            instrument=selection.instrument,
            contract=selection.contract,
            trigger_key=trigger_key,
        )

    # -- Emergency (manual button) trigger -------------------------------------------------

    def trigger_emergency_flatten(
        self,
        *,
        account: TradingAccount,
        instrument: Instrument,
        contract: ContractMonth,
        confirmed_net: NetPosition,
    ) -> EodFlattenWorkflowRecord:
        """The emergency-flatten button's backing call. `confirmed_net` must be exactly
        what the operator most recently confirmed on screen (account/契約/口數) — this
        re-queries the broker fresh and raises `StaleEmergencyConfirmationError` if the
        position has moved again since, never trusting the caller's number alone (same
        posture as `PositionReconciliationService.confirm_manual_sync`). Immediately
        forces the strategy toward a safe pause regardless of its current state, before
        anything else — "立即暫停策略並啟動同一安全 flatten workflow"."""
        with self._lock:
            attempt_safe_pause(self._strategy_state_machine)
            positions = self._trade_gateway.query_positions()
            matching = _find_position(positions, account, instrument, contract)
            fresh_net = matching.net if matching is not None else NetPosition(0)
            if fresh_net.lots != confirmed_net.lots:
                raise StaleEmergencyConfirmationError(
                    "券商持倉已再次變動，先前確認的口數已過期，請重新查詢後再次確認"
                )
            # A fresh, unique key every call (never derived from wall-clock time, which
            # two rapid-fire calls in the same second could collide on) — "already
            # active" is enforced by the `find_active_for_contract` check inside
            # `_begin`, not by trigger-key dedup, for this trigger kind.
            trigger_key = f"emergency:{instrument.value}:{contract.code}:{uuid4()}"
            record = self._begin(
                trigger=EodFlattenTrigger.EMERGENCY,
                account=account,
                instrument=instrument,
                contract=contract,
                trigger_key=trigger_key,
            )
            if record is None:
                raise EodFlattenAlreadyActiveError(
                    f"{instrument.value} {contract.code} 已有進行中的平倉 workflow"
                )
            return record

    def active_workflow_for(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> EodFlattenWorkflowRecord | None:
        """The in-flight flatten workflow (if any) for this account/contract — lets the
        emergency-flatten UI recover and display an already-active workflow's state
        after a restart, without needing direct persistence-layer access (at most one
        active workflow ever exists per contract, enforced by `_begin`)."""
        active = self._repo.find_active_for_contract(account, instrument, contract)
        return active[0] if active else None

    # -- Shared start/advance machinery ----------------------------------------------------

    def _begin(
        self,
        *,
        trigger: EodFlattenTrigger,
        account: TradingAccount,
        instrument: Instrument,
        contract: ContractMonth,
        trigger_key: str,
    ) -> EodFlattenWorkflowRecord | None:
        existing = self._repo.find_by_trigger_key(trigger_key)
        if existing is not None:
            log_info(
                _logger,
                "eod_flatten_submit_deduped",
                trigger_key=trigger_key,
                workflow_id=str(existing.workflow_id.value),
                state=existing.state.value,
            )
            return existing
        active = self._repo.find_active_for_contract(account, instrument, contract)
        if active:
            log_warning(
                _logger,
                "eod_flatten_trigger_blocked_already_active",
                trigger=trigger.value,
                trigger_key=trigger_key,
                instrument=instrument.value,
                contract=contract.code,
                active_workflow_id=str(active[0].workflow_id.value),
            )
            return None
        now = self._clock.now()
        record = EodFlattenWorkflowRecord(
            workflow_id=EodFlattenWorkflowId(),
            trigger_key=trigger_key,
            trigger=trigger,
            account=account,
            instrument=instrument,
            contract=contract,
            state=EodFlattenWorkflowState.STARTED,
            created_at=now,
            updated_at=now,
        )
        outcome = self._repo.save(record)
        if outcome is EodFlattenWorkflowSaveOutcome.DUPLICATE_KEY:
            raced = self._repo.find_by_trigger_key(trigger_key)
            assert raced is not None
            return raced
        log_info(
            _logger,
            "eod_flatten_workflow_started",
            workflow_id=str(record.workflow_id.value),
            trigger=trigger.value,
            account_no=mask_account(account.account_no),
            instrument=instrument.value,
            contract=contract.code,
            trigger_key=trigger_key,
        )
        self._publish(
            EodFlattenWorkflowStarted(
                at=now,
                workflow_id=record.workflow_id,
                trigger=trigger,
                account=account,
                instrument=instrument,
                contract=contract,
                trigger_key=trigger_key,
            )
        )
        return self._advance(record)

    def _advance(self, record: EodFlattenWorkflowRecord) -> EodFlattenWorkflowRecord:
        if record.state in (
            EodFlattenWorkflowState.STARTED,
            EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS,
        ):
            return self._advance_from_started_or_waiting(record)
        if record.state is EodFlattenWorkflowState.POSITION_QUERIED:
            return self._advance_from_position_queried(record)
        if record.state is EodFlattenWorkflowState.CLOSE_FILLED_BY_REPORT:
            return self._advance_from_close_filled(record)
        return record  # CLOSE_ORDER_SUBMITTED: event-driven only

    def _advance_from_started_or_waiting(
        self, record: EodFlattenWorkflowRecord
    ) -> EodFlattenWorkflowRecord:
        active_orders = self._order_repository.find_active_for_contract(
            record.account, record.instrument, record.contract
        )
        now = self._clock.now()
        if active_orders:
            if record.state is EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS:
                log_info(
                    _logger,
                    "eod_flatten_still_waiting_active_orders",
                    workflow_id=str(record.workflow_id.value),
                    active_order_count=len(active_orders),
                )
                return record
            updated = EodFlattenWorkflowStateMachine(record).mark_waiting_active_orders(at=now)
            self._repo.update(updated)
            log_warning(
                _logger,
                "eod_flatten_waiting_active_orders",
                workflow_id=str(updated.workflow_id.value),
                active_order_count=len(active_orders),
            )
            return updated

        positions = self._trade_gateway.query_positions()
        matching = _find_position(positions, record.account, record.instrument, record.contract)
        net = matching.net if matching is not None else NetPosition(0)
        if net.lots == 0:
            machine = EodFlattenWorkflowStateMachine(record)
            updated = machine.mark_already_flat(at=now)
            self._repo.update(updated)
            log_info(
                _logger, "eod_flatten_already_flat", workflow_id=str(updated.workflow_id.value)
            )
            return updated
        updated = EodFlattenWorkflowStateMachine(record).mark_position_queried(
            starting_net=net, at=now
        )
        self._repo.update(updated)
        log_info(
            _logger,
            "eod_flatten_position_queried",
            workflow_id=str(updated.workflow_id.value),
            starting_net=net.lots,
            close_side=updated.close_side.value if updated.close_side else None,
        )
        return self._advance(updated)

    def _advance_from_position_queried(
        self, record: EodFlattenWorkflowRecord
    ) -> EodFlattenWorkflowRecord:
        assert record.starting_net is not None
        assert record.close_side is not None
        price = self._last_price.get((record.instrument, record.contract))
        if price is None:
            return self._pause(
                record, reason="尚無可靠成交價格（尚未收到任何K棒收盤），無法送出平倉委託"
            )
        request = OrderRequest(
            account=record.account,
            instrument=record.instrument,
            contract=record.contract,
            side=record.close_side,
            quantity=Quantity(abs(record.starting_net.lots)),
            price=Price(price),
            kind=OrderKind.CLOSE,
            time_in_force=TimeInForce.ROD,
            idempotency_key=f"{record.workflow_id.value}:close",
            workflow_id=str(record.workflow_id.value),
            reason=f"eod flatten workflow {record.workflow_id.value} ({record.trigger.value})",
        )
        try:
            order_intent = self._order_manager.submit(request)
        except (ActiveWorkflowInProgressError, OrderExposureExceededError) as exc:
            return self._pause(record, reason=f"平倉委託送出失敗：{exc}")
        now = self._clock.now()
        updated = EodFlattenWorkflowStateMachine(record).mark_close_submitted(
            client_order_id=order_intent.client_order_id, at=now
        )
        self._repo.update(updated)
        self._tracked[order_intent.client_order_id] = updated.workflow_id
        log_info(
            _logger,
            "eod_flatten_close_order_submitted",
            workflow_id=str(updated.workflow_id.value),
            client_order_id=str(order_intent.client_order_id.value),
            quantity=request.quantity.lots,
            side=request.side.value,
        )
        return updated

    def _advance_from_close_filled(
        self, record: EodFlattenWorkflowRecord
    ) -> EodFlattenWorkflowRecord:
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
                "eod_flatten_final_confirmation_failed",
                workflow_id=str(updated.workflow_id.value),
                is_flat=result.is_flat,
                position_lots=result.position_lots,
                has_active_or_unknown_orders=result.has_active_or_unknown_orders,
                session_healthy=result.session_healthy,
                market_data_healthy=result.market_data_healthy,
            )
            return updated
        updated = EodFlattenWorkflowStateMachine(record).mark_completed(at=now)
        self._repo.update(updated)
        self._untrack(updated)
        log_info(_logger, "eod_flatten_completed", workflow_id=str(updated.workflow_id.value))
        self._publish(EodFlattenCompleted(at=now, workflow_id=updated.workflow_id))
        return updated

    def _pause(
        self, record: EodFlattenWorkflowRecord, *, reason: str, at: Timestamp | None = None
    ) -> EodFlattenWorkflowRecord:
        now = at if at is not None else self._clock.now()
        updated = EodFlattenWorkflowStateMachine(record).mark_paused(reason=reason, at=now)
        self._repo.update(updated)
        self._untrack(updated)
        log_error(
            _logger,
            "eod_flatten_paused_safe",
            workflow_id=str(updated.workflow_id.value),
            reason=reason,
        )
        self._publish(
            EodFlattenPausedSafe(
                at=now, workflow_id=updated.workflow_id, state=updated.state, reason=reason
            )
        )
        return updated

    # -- Event handlers ---------------------------------------------------------------------

    def _on_bar_closed(self, event: BarClosed) -> None:
        with self._lock:
            self._last_price[(event.instrument, event.contract)] = event.bar.close.amount

    def _on_order_state_transitioned(self, event: OrderStateTransitioned) -> None:
        with self._lock:
            workflow_id = self._tracked.get(event.client_order_id)
            if workflow_id is None:
                return
            record = self._repo.find_by_workflow_id(workflow_id)
            if record is None or record.state is EodFlattenWorkflowState.PAUSED_SAFE:
                return
            if event.to_status is OrderStatus.FILLED:
                updated = EodFlattenWorkflowStateMachine(record).mark_close_filled(at=event.at)
                self._repo.update(updated)
                log_info(
                    _logger,
                    "eod_flatten_close_order_filled",
                    workflow_id=str(updated.workflow_id.value),
                )
                self._advance(updated)
            elif event.to_status is OrderStatus.CANCELLED:
                # This service never itself cancels a tracked order — an observed
                # cancellation here is always unexpected.
                self._pause(
                    record,
                    reason=f"連結委託 {event.client_order_id.value} 非預期地被取消",
                    at=event.at,
                )

    def _on_order_requires_manual_review(self, event: OrderRequiresManualReview) -> None:
        with self._lock:
            workflow_id = self._tracked.get(event.client_order_id)
            if workflow_id is None:
                return
            record = self._repo.find_by_workflow_id(workflow_id)
            if record is None or record.state is EodFlattenWorkflowState.PAUSED_SAFE:
                return
            reason = (
                f"連結委託 {event.client_order_id.value} 進入 {event.status.value}：{event.reason}"
            )
            self._pause(record, reason=reason, at=event.at)

    def _on_session_ready(self, event: BrokerSessionReady) -> None:
        with self._lock:
            self._reload_tracked()
        if not self._startup_safety_checked:
            self._startup_safety_checked = True
            self._check_startup_safety(event.account)
        self.on_clock_tick()

    # -- Startup-after-04:55 safety check ---------------------------------------------------

    def _check_startup_safety(self, account: TradingAccount) -> None:
        """Never auto-flattens — see the module docstring's "never auto-flattens a
        position discovered already open at startup" note. Only forces a safe pause and
        publishes a high-priority alert; the operator must use the emergency-flatten
        control to actually close the position."""
        selection = self._current_selection()
        if selection is None:
            return
        now = self._clock.now()
        if not is_within_no_entry_window(
            now,
            eod_flatten_local_time=self._eod_flatten_local_time,
            entry_gate_local_time=self._entry_gate_local_time,
        ):
            return
        try:
            positions = self._trade_gateway.query_positions()
        except Exception as exc:  # noqa: BLE001 - a query failure must never crash startup
            log_error(_logger, "startup_position_safety_query_failed", error=str(exc))
            return
        matching = _find_position(positions, account, selection.instrument, selection.contract)
        net = matching.net if matching is not None else NetPosition(0)
        if net.lots == 0:
            return
        resulting = attempt_safe_pause(self._strategy_state_machine)
        log_error(
            _logger,
            "startup_position_safety_pause_triggered",
            account_no=mask_account(account.account_no),
            instrument=selection.instrument.value,
            contract=selection.contract.code,
            net_lots=net.lots,
            resulting_strategy_state=resulting.value if resulting else None,
        )
        self._publish(
            StartupPositionSafetyPauseTriggered(
                at=now,
                account=account,
                instrument=selection.instrument,
                contract=selection.contract,
                net=net,
                resulting_strategy_state=resulting.value if resulting else None,
            )
        )

    # -- Restart / reconnect recovery --------------------------------------------------------

    def _resume_one(self, record: EodFlattenWorkflowRecord) -> None:
        if record.state in (
            EodFlattenWorkflowState.STARTED,
            EodFlattenWorkflowState.WAITING_ACTIVE_ORDERS,
            EodFlattenWorkflowState.POSITION_QUERIED,
            EodFlattenWorkflowState.CLOSE_FILLED_BY_REPORT,
        ):
            self._advance(record)
            return
        if record.state is EodFlattenWorkflowState.CLOSE_ORDER_SUBMITTED:
            assert record.close_client_order_id is not None
            self._resume_linked_order(record, record.close_client_order_id)
            return

    def _resume_linked_order(
        self, record: EodFlattenWorkflowRecord, client_order_id: ClientOrderId
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
            updated = EodFlattenWorkflowStateMachine(record).mark_close_filled(at=now)
            self._repo.update(updated)
            log_info(
                _logger,
                "eod_flatten_close_order_filled",
                workflow_id=str(updated.workflow_id.value),
                resumed=True,
            )
            self._advance(updated)
            return
        if order.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN, OrderStatus.CANCELLED):
            reason = f"重啟後查詢連結委託狀態為 {order.status.value}"
            self._pause(record, reason=reason, at=now)
            return
        # Still genuinely in flight (SUBMITTING/ACKNOWLEDGED/PARTIALLY_FILLED/
        # CANCEL_PENDING) — leave it; OrderManager's own reconciliation (already run
        # earlier at startup) will fire the events that continue driving this workflow.

    def _reload_tracked(self) -> None:
        tracked: dict[ClientOrderId, EodFlattenWorkflowId] = {}
        for record in self._repo.list_active():
            if record.state is EodFlattenWorkflowState.PAUSED_SAFE:
                continue
            if record.close_client_order_id is not None:
                tracked[record.close_client_order_id] = record.workflow_id
        self._tracked = tracked

    # -- Internal helpers ---------------------------------------------------------------

    def _untrack(self, record: EodFlattenWorkflowRecord) -> None:
        if record.close_client_order_id is not None:
            self._tracked.pop(record.close_client_order_id, None)

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


__all__ = ["RiskSupervisor"]
