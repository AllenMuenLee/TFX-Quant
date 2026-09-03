"""PositionReconciliationService — Feature 08's "independent Python service" that
continuously compares the broker's actual position (and active/unknown orders) against
this system's own locally-derived expected position, at every required trigger point,
and safely pauses the strategy on any discrepancy before any automatic new order can be
sent.

Real position truth only ever comes from `TradeGatewayPort.query_positions()`, queried
fresh at every trigger — never cached, never inferred from a fill report alone (a fill
report only ever *updates the expected baseline*, it never substitutes for a real
query). A discrepancy is never auto-explained as a strategy fill and never auto-
"corrected" by sending an order — the only way the expected baseline changes is (a) an
incremental update from this system's own observed fill, or (b) an explicit, human-
confirmed `confirm_manual_sync()` call. See `docs/adr/0010-position-reconciliation-and-
manual-sync.md`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BrokerSessionReady,
    Event,
    FillReceived,
    ManualPositionSyncCompleted,
    PositionDiscrepancyDetected,
    ReversalFlatConfirmed,
)
from tfx_quant.application.ports.bar_signal_state import BarSignalStateStore
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.position_baseline_repository import PositionBaselineRepository
from tfx_quant.application.ports.reversal_workflow_repository import ReversalWorkflowRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.application.position_reconciliation.errors import (
    ManualSyncBlockedError,
    StaleSyncConfirmationError,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.position_reconciliation import (
    SUSPECTED_CAUSE_HYPOTHESES,
    DiscrepancyKind,
    ManualSyncPreflight,
    ManualSyncRecord,
    PositionBaseline,
    ReconciliationRecord,
    ReconciliationTrigger,
    classify_discrepancy,
)
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reversal_workflow import ReversalWorkflowState, ReversalWorkflowStateMachine
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine, attempt_safe_pause
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import (
    correlation_scope,
    current_correlation_id,
    get_logger,
    log_error,
    log_info,
    log_warning,
    new_correlation_id,
)
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 60.0


class Selection(Protocol):
    """Structural stand-in for `application.instrument_selection.selection.
    ResolvedSelection` — this service only ever reads `instrument`/`contract` off
    whatever "currently selected" object composition wires in, so it depends on that
    shape rather than the concrete type (same seam style as `ReversalWorkflowService`'s
    `market_data_healthy` callable avoiding a direct `MarketDataBarService` dependency)."""

    @property
    def instrument(self) -> Instrument: ...

    @property
    def contract(self) -> ContractMonth: ...


CurrentSelection = Callable[[], Selection | None]
SelectedAccount = Callable[[], TradingAccount | None]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as `application.
    order_management.order_manager.EventBus`."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


class PositionReconciliationService:
    def __init__(
        self,
        *,
        trade_gateway: TradeGatewayPort,
        order_repository: OrderRepository,
        reversal_workflow_repository: ReversalWorkflowRepository,
        baseline_repository: PositionBaselineRepository,
        bar_signal_state_store: BarSignalStateStore,
        strategy_state_machine: StrategyStateMachine,
        clock: Clock,
        event_bus: EventBus,
        current_selection: CurrentSelection,
        selected_account: SelectedAccount,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._trade_gateway = trade_gateway
        self._order_repository = order_repository
        self._reversal_workflow_repository = reversal_workflow_repository
        self._baseline_repository = baseline_repository
        self._bar_signal_state_store = bar_signal_state_store
        self._strategy_state_machine = strategy_state_machine
        self._clock = clock
        self._event_bus = event_bus
        self._current_selection = current_selection
        self._selected_account = selected_account
        self._poll_interval_seconds = poll_interval_seconds
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._running = False
        self._has_completed_first_session_ready = False
        self._last_record: ReconciliationRecord | None = None

        event_bus.subscribe(FillReceived, self._on_fill)
        event_bus.subscribe(BrokerSessionReady, self._on_session_ready)
        event_bus.subscribe(ReversalFlatConfirmed, self._on_reversal_flat_confirmed)

    # -- Lifecycle: the timed-poll trigger ---------------------------------------------

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
        timer = threading.Timer(self._poll_interval_seconds, self._on_timer_fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer_fire(self) -> None:
        self.on_clock_tick()
        with self._lock:
            if self._running:
                self._schedule_next_tick()

    def on_clock_tick(self) -> None:
        """Public so tests can drive the timed-poll trigger directly, without a real
        timer — same convention as `OrderManager.on_clock_tick`."""
        self.reconcile(trigger=ReconciliationTrigger.TIMED_POLL)

    @property
    def last_record(self) -> ReconciliationRecord | None:
        """Most recent automatic broker/local comparison for read-only UI display."""
        with self._lock:
            return self._last_record

    # -- OrderManager's position_lookup seam ---------------------------------------------

    def expected_net_lookup(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> NetPosition:
        """Replaces Feature 06's always-flat `desktop.composition._flat_position_lookup`
        placeholder — wired as `OrderManager`'s `position_lookup`."""
        baseline = self._baseline_repository.get(account, instrument, contract)
        return baseline.expected_net if baseline is not None else NetPosition(0)

    # -- Explicit trigger entry points ---------------------------------------------------

    def on_strategy_start(self) -> ReconciliationRecord | None:
        """Documented wiring point for "每次...啟動策略...查詢持倉": no code path in this
        codebase transitions `StrategyState` into `RUNNING` yet (Feature 05/12's job,
        same gap as `application.instrument_selection`'s "why this service never
        transitions StrategyStateMachine itself") — whichever future service does so is
        expected to call this first."""
        return self.reconcile(trigger=ReconciliationTrigger.STRATEGY_START)

    def on_foreground_return(self) -> ReconciliationRecord | None:
        """Wired from `desktop.app.TfxQuantApp`'s `wx.EVT_ACTIVATE_APP` handler."""
        return self.reconcile(trigger=ReconciliationTrigger.FOREGROUND_RETURN)

    def request_manual_requery(self) -> ReconciliationRecord | None:
        """The two-step manual-sync flow's first step ("重新查詢") — also the query the
        confirmation button's account/instrument/contract/actual-lots/time display is
        built from."""
        return self.reconcile(trigger=ReconciliationTrigger.MANUAL_REQUERY)

    # -- Event-driven triggers -----------------------------------------------------------

    def _on_fill(self, event: FillReceived) -> None:
        fill = event.fill
        order = self._order_repository.find_by_client_order_id(fill.client_order_id)
        if order is None:
            log_error(
                _logger,
                "reconciliation_fill_unmatched",
                client_order_id=str(fill.client_order_id.value),
                broker_fill_no=fill.broker_fill_no,
            )
            return
        delta = fill.quantity.lots if fill.side is Side.BUY else -fill.quantity.lots
        baseline = self._baseline_repository.get(order.account, order.instrument, order.contract)
        prior_lots = baseline.expected_net.lots if baseline is not None else 0
        new_baseline = PositionBaseline(
            account=order.account,
            instrument=order.instrument,
            contract=order.contract,
            expected_net=NetPosition(prior_lots + delta),
            updated_at=event.at,
            source="fill",
        )
        self._baseline_repository.upsert(new_baseline)
        log_info(
            _logger,
            "position_baseline_updated_from_fill",
            instrument=order.instrument.value,
            contract=order.contract.code,
            prior_net=prior_lots,
            delta=delta,
            new_net=new_baseline.expected_net.lots,
            broker_fill_no=fill.broker_fill_no,
        )
        self.reconcile(
            trigger=ReconciliationTrigger.FILL,
            account=order.account,
            instrument=order.instrument,
            contract=order.contract,
        )

    def _on_session_ready(self, event: BrokerSessionReady) -> None:
        trigger = (
            ReconciliationTrigger.RECONNECT
            if self._has_completed_first_session_ready
            else ReconciliationTrigger.LOGIN
        )
        self._has_completed_first_session_ready = True
        self.reconcile(trigger=trigger, account=event.account)

    def _on_reversal_flat_confirmed(self, event: ReversalFlatConfirmed) -> None:
        record = self._reversal_workflow_repository.find_by_workflow_id(event.workflow_id)
        if record is None:
            return
        self.reconcile(
            trigger=ReconciliationTrigger.REVERSAL_FLAT_GATE,
            account=record.account,
            instrument=record.instrument,
            contract=record.contract,
        )

    # -- Core reconciliation --------------------------------------------------------------

    def reconcile(
        self,
        *,
        trigger: ReconciliationTrigger,
        account: TradingAccount | None = None,
        instrument: Instrument | None = None,
        contract: ContractMonth | None = None,
    ) -> ReconciliationRecord | None:
        with correlation_scope(correlation_id=new_correlation_id()):
            correlation_id = current_correlation_id() or ""
            target = self._resolve_target(account, instrument, contract)
            if target is None:
                log_info(_logger, "reconciliation_skipped_no_selection", trigger=trigger.value)
                return None
            resolved_account, resolved_instrument, resolved_contract = target
            with self._lock:
                record = self._run(
                    trigger,
                    resolved_account,
                    resolved_instrument,
                    resolved_contract,
                    correlation_id,
                )
                self._last_record = record
                return record

    def _resolve_target(
        self,
        account: TradingAccount | None,
        instrument: Instrument | None,
        contract: ContractMonth | None,
    ) -> tuple[TradingAccount, Instrument, ContractMonth] | None:
        if instrument is None or contract is None:
            selection = self._current_selection()
            if selection is None:
                return None
            instrument = instrument if instrument is not None else selection.instrument
            contract = contract if contract is not None else selection.contract
        if account is None:
            account = self._selected_account()
            if account is None:
                return None
        return account, instrument, contract

    def _run(
        self,
        trigger: ReconciliationTrigger,
        account: TradingAccount,
        instrument: Instrument,
        contract: ContractMonth,
        correlation_id: str,
    ) -> ReconciliationRecord:
        query_id = new_correlation_id()
        now = self._clock.now()
        masked_account = mask_account(account.account_no)
        expected_net = self._expected_net(account, instrument, contract)
        start = time.monotonic()
        try:
            positions = self._trade_gateway.query_positions()
        except Exception as exc:  # noqa: BLE001 - a query failure must never crash the caller
            duration = time.monotonic() - start
            log_error(
                _logger,
                "position_query_failed",
                query_id=query_id,
                correlation_id=correlation_id,
                trigger=trigger.value,
                account_no=masked_account,
                instrument=instrument.value,
                contract=contract.code,
                duration_seconds=duration,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            current_state = self._strategy_state_machine.state
            query_failure_state = (
                attempt_safe_pause(self._strategy_state_machine)
                if current_state is StrategyState.RUNNING
                else current_state
            )
            return ReconciliationRecord(
                correlation_id=correlation_id,
                query_id=query_id,
                trigger=trigger,
                account=account,
                instrument=instrument,
                contract=contract,
                expected_net=expected_net,
                actual_net=None,
                broker_snapshot_at=None,
                discrepancy=DiscrepancyKind.NONE,
                has_active_or_unknown_orders=False,
                active_or_unknown_order_count=0,
                other_contract_position_count=0,
                paused=True,
                resulting_strategy_state=(
                    query_failure_state.value if query_failure_state is not None else None
                ),
                possible_causes=(),
                query_duration_seconds=duration,
                query_error=str(exc),
                at=now,
            )
        duration = time.monotonic() - start

        matching = _find_position(positions, account, instrument, contract)
        actual_net = matching.net if matching is not None else NetPosition(0)
        broker_snapshot_at = matching.as_of if matching is not None else None
        other_positions = [
            p
            for p in positions
            if p.account == account and (p.instrument, p.contract) != (instrument, contract)
        ]
        active_orders = self._order_repository.find_active_for_contract(
            account, instrument, contract
        )
        has_active_or_unknown = bool(active_orders)
        discrepancy = classify_discrepancy(expected_net, actual_net)
        if discrepancy is DiscrepancyKind.NONE and any(
            not position.net.is_flat for position in other_positions
        ):
            discrepancy = DiscrepancyKind.OTHER_CONTRACT

        log_info(
            _logger,
            "position_query_completed",
            query_id=query_id,
            correlation_id=correlation_id,
            trigger=trigger.value,
            account_no=masked_account,
            instrument=instrument.value,
            contract=contract.code,
            broker_snapshot_at=broker_snapshot_at.value.isoformat() if broker_snapshot_at else None,
            returned_count=len(positions),
            net_lots=actual_net.lots,
            duration_seconds=duration,
        )

        resulting_state: StrategyState | None = None
        paused = False
        possible_causes: tuple[str, ...] = ()
        if discrepancy is not DiscrepancyKind.NONE:
            possible_causes = SUSPECTED_CAUSE_HYPOTHESES
            paused = True
            # Only actually attempt a transition from RUNNING — repeatedly detecting the
            # *same still-unresolved* mismatch on every subsequent trigger (poll,
            # requery, ...) must not progressively escalate PAUSED_SAFE -> FAULTED; it
            # should just keep reporting the strategy is already safely stopped.
            current_state = self._strategy_state_machine.state
            resulting_state = (
                attempt_safe_pause(self._strategy_state_machine)
                if current_state is StrategyState.RUNNING
                else current_state
            )
            log_error(
                _logger,
                "reconciliation_mismatch_detected",
                query_id=query_id,
                correlation_id=correlation_id,
                trigger=trigger.value,
                account_no=masked_account,
                instrument=instrument.value,
                contract=contract.code,
                expected_net=expected_net.lots,
                actual_net=actual_net.lots,
                discrepancy=discrepancy.value,
                has_active_or_unknown_orders=has_active_or_unknown,
                active_or_unknown_order_count=len(active_orders),
                possible_causes=list(possible_causes),
                resulting_strategy_state=resulting_state.value if resulting_state else None,
                current_strategy_state=self._strategy_state_machine.state.value,
            )
            self._publish(
                PositionDiscrepancyDetected(
                    at=now,
                    trigger=trigger,
                    account=account,
                    instrument=instrument,
                    contract=contract,
                    expected_net=expected_net,
                    actual_net=actual_net,
                    discrepancy=discrepancy,
                    resulting_strategy_state=resulting_state,
                    correlation_id=correlation_id,
                )
            )
        else:
            log_info(
                _logger,
                "reconciliation_matched",
                query_id=query_id,
                correlation_id=correlation_id,
                trigger=trigger.value,
                account_no=masked_account,
                instrument=instrument.value,
                contract=contract.code,
                expected_net=expected_net.lots,
                actual_net=actual_net.lots,
                has_active_or_unknown_orders=has_active_or_unknown,
                active_or_unknown_order_count=len(active_orders),
            )

        if other_positions:
            log_warning(
                _logger,
                "reconciliation_other_contract_positions_detected",
                query_id=query_id,
                correlation_id=correlation_id,
                account_no=masked_account,
                other_positions=[
                    {
                        "instrument": p.instrument.value,
                        "contract": p.contract.code,
                        "net_lots": p.net.lots,
                    }
                    for p in other_positions
                ],
            )

        return ReconciliationRecord(
            correlation_id=correlation_id,
            query_id=query_id,
            trigger=trigger,
            account=account,
            instrument=instrument,
            contract=contract,
            expected_net=expected_net,
            actual_net=actual_net,
            broker_snapshot_at=broker_snapshot_at,
            discrepancy=discrepancy,
            has_active_or_unknown_orders=has_active_or_unknown,
            active_or_unknown_order_count=len(active_orders),
            other_contract_position_count=len(other_positions),
            paused=paused,
            resulting_strategy_state=resulting_state.value if resulting_state else None,
            possible_causes=possible_causes,
            query_duration_seconds=duration,
            query_error=None,
            at=now,
        )

    def _expected_net(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> NetPosition:
        baseline = self._baseline_repository.get(account, instrument, contract)
        return baseline.expected_net if baseline is not None else NetPosition(0)

    # -- Manual sync (two-step: request_manual_requery() above, then this) --------------

    def confirm_manual_sync(
        self,
        *,
        confirmed_actual_net: NetPosition,
        confirmed_query_at: Timestamp,
        account: TradingAccount | None = None,
        instrument: Instrument | None = None,
        contract: ContractMonth | None = None,
    ) -> ManualSyncRecord:
        """The two-step flow's second step ("確認以元大實際持倉同步"). `confirmed_actual_net`/
        `confirmed_query_at` must be exactly what `request_manual_requery()` most recently
        displayed to the operator — this method re-queries the broker fresh (never trusts
        the caller's number alone) and rejects the confirmation as stale
        (`StaleSyncConfirmationError`) if the broker position has moved again since."""
        with correlation_scope(correlation_id=new_correlation_id()):
            correlation_id = current_correlation_id() or ""
            target = self._resolve_target(account, instrument, contract)
            if target is None:
                raise ManualSyncBlockedError("尚未選定商品／契約或帳號，無法同步")
            resolved_account, resolved_instrument, resolved_contract = target
            with self._lock:
                return self._confirm_manual_sync(
                    resolved_account,
                    resolved_instrument,
                    resolved_contract,
                    confirmed_actual_net=confirmed_actual_net,
                    confirmed_query_at=confirmed_query_at,
                    correlation_id=correlation_id,
                )

    def _confirm_manual_sync(
        self,
        account: TradingAccount,
        instrument: Instrument,
        contract: ContractMonth,
        *,
        confirmed_actual_net: NetPosition,
        confirmed_query_at: Timestamp,
        correlation_id: str,
    ) -> ManualSyncRecord:
        masked_account = mask_account(account.account_no)
        active_orders = self._order_repository.find_active_for_contract(
            account, instrument, contract
        )
        preflight = ManualSyncPreflight(
            has_active_orders=any(
                o.is_active and o.status is not OrderStatus.UNKNOWN for o in active_orders
            ),
            has_unknown_orders=any(o.status is OrderStatus.UNKNOWN for o in active_orders),
        )
        log_info(
            _logger,
            "manual_sync_preflight_evaluated",
            correlation_id=correlation_id,
            account_no=masked_account,
            instrument=instrument.value,
            contract=contract.code,
            has_active_orders=preflight.has_active_orders,
            has_unknown_orders=preflight.has_unknown_orders,
            allowed=preflight.allowed,
        )
        if not preflight.allowed:
            reason = (
                "仍有狀態不明委託，禁止同步，須先由人工在券商端釐清"
                if preflight.has_unknown_orders
                else "仍有活動委託，禁止同步"
            )
            raise ManualSyncBlockedError(reason)

        now = self._clock.now()
        try:
            positions = self._trade_gateway.query_positions()
        except Exception as exc:
            log_error(
                _logger,
                "manual_sync_confirmation_query_failed",
                correlation_id=correlation_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise ManualSyncBlockedError(f"重新查詢持倉失敗，無法同步：{exc}") from exc

        matching = _find_position(positions, account, instrument, contract)
        fresh_actual = matching.net if matching is not None else NetPosition(0)
        fresh_snapshot_at = matching.as_of if matching is not None else now
        log_info(
            _logger,
            "manual_sync_confirmation_received",
            correlation_id=correlation_id,
            account_no=masked_account,
            instrument=instrument.value,
            contract=contract.code,
            confirmed_net=confirmed_actual_net.lots,
            confirmed_at=confirmed_query_at.value.isoformat(),
            fresh_net=fresh_actual.lots,
            fresh_snapshot_at=fresh_snapshot_at.value.isoformat(),
        )
        if fresh_actual.lots != confirmed_actual_net.lots:
            raise StaleSyncConfirmationError(
                "券商持倉已再次變動，先前確認的內容已過期，請重新查詢後再次確認同步"
            )

        baseline_before_record = self._baseline_repository.get(account, instrument, contract)
        baseline_before = (
            baseline_before_record.expected_net
            if baseline_before_record is not None
            else NetPosition(0)
        )
        new_baseline = PositionBaseline(
            account=account,
            instrument=instrument,
            contract=contract,
            expected_net=fresh_actual,
            updated_at=now,
            source="manual_sync",
        )
        self._baseline_repository.upsert(new_baseline)

        self._bar_signal_state_store.clear(instrument, contract)
        reversal_reset = self._reset_paused_reversal_workflows(
            account, instrument, contract, correlation_id=correlation_id, at=now
        )

        resulting_state = self._strategy_state_machine.state
        still_paused = resulting_state is StrategyState.PAUSED_SAFE

        log_info(
            _logger,
            "manual_sync_completed",
            correlation_id=correlation_id,
            account_no=masked_account,
            instrument=instrument.value,
            contract=contract.code,
            baseline_before=baseline_before.lots,
            baseline_after=fresh_actual.lots,
            bar_signal_state_reset=True,
            reversal_workflow_reset=reversal_reset,
            resulting_strategy_state=resulting_state.value,
            still_paused_safe=still_paused,
        )
        self._publish(
            ManualPositionSyncCompleted(
                at=now,
                account=account,
                instrument=instrument,
                contract=contract,
                baseline_before=baseline_before,
                baseline_after=fresh_actual,
                correlation_id=correlation_id,
            )
        )
        return ManualSyncRecord(
            correlation_id=correlation_id,
            account=account,
            instrument=instrument,
            contract=contract,
            baseline_before=baseline_before,
            baseline_after=fresh_actual,
            broker_snapshot_at=fresh_snapshot_at,
            bar_signal_state_reset=True,
            reversal_workflow_reset=reversal_reset,
            resulting_strategy_state=resulting_state.value,
            still_paused_safe=still_paused,
            at=now,
        )

    def _reset_paused_reversal_workflows(
        self,
        account: TradingAccount,
        instrument: Instrument,
        contract: ContractMonth,
        *,
        correlation_id: str,
        at: Timestamp,
    ) -> bool:
        """Retires (never resumes) any `PAUSED_SAFE` reversal workflow left on this
        contract so a fresh reversal can start once the operator restarts the strategy —
        see `domain.reversal_workflow`'s narrowly-scoped `PAUSED_SAFE -> BLOCKED`
        exception, added specifically for this call site."""
        active = self._reversal_workflow_repository.find_active_for_contract(
            account, instrument, contract
        )
        reset_any = False
        for record in active:
            if record.state is not ReversalWorkflowState.PAUSED_SAFE:
                continue
            updated = ReversalWorkflowStateMachine(record).mark_blocked(
                reason=f"position reconciliation 人工同步重置（correlation_id={correlation_id}）",
                at=at,
            )
            self._reversal_workflow_repository.update(updated)
            reset_any = True
            log_info(
                _logger,
                "reversal_workflow_reset_by_manual_sync",
                workflow_id=str(updated.workflow_id.value),
                correlation_id=correlation_id,
            )
        return reset_any

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


__all__ = ["PositionReconciliationService"]
