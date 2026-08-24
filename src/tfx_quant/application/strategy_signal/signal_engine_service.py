"""StrategySignalEngineService — wires `domain.strategy_signal_engine.StrategySignalEngine`
to live events and to `OrderManager`, the only component allowed to submit a real order
(implementation prompt 05: "引擎只接收...事件，輸出...交易意圖；不得直接呼叫券商API").

One `StrategySignalEngine` instance is kept per `(Instrument, ContractMonth)`, created
lazily on first `BarClosed`. This class itself implements `application.ports.
bar_signal_state.BarSignalStateStore` — `clear()` drops that pair's engine (and every
flag tracked for it), which is exactly Feature 03's instrument-switch reset hook and
Feature 08's manual-sync reset hook, now with a real body instead of
`NullBarSignalStateStore`'s no-op.

**Order price**: this codebase's market data is yfinance-hourly-poll only (see
`docs/adr/0006`) — there is no live tick/quote feed anywhere, so the *only* price this
engine (or this service) ever has is the triggering bar's close, carried on every
`StrategyDecision.current_price`. Every submitted order uses that price.

**Submission path**: every signal (`ENTER_*`/`ADD_*`/`EXIT_ALL`) goes straight through
`OrderManager.submit()`, never through `ScalingService`/`ReversalWorkflowService`. Those
two re-derive their own gate state from a fresh broker position query; this engine has
already performed the equivalent (and, for this feature's rules, authoritative) gating
from its own fill-confirmed position tracking, so routing through them would be a second,
differently-sourced gate check on top of an already-decided signal — not a safety
improvement, just two sources of truth. `OrderManager` itself still independently
re-validates exposure/active-order state before ever calling the gateway, so this is not
a bypass of any actual safety check.

`ActiveWorkflowInProgressError`/`OrderExposureExceededError` from that final
`OrderManager` check are caught and logged as an expected, benign race (this service's
own `has_active_order` snapshot and the moment of `submit()` are not atomic) — same
"ordinary rejection, not a bug" treatment `ScalingService` gives its own gate. Any other
exception is left to propagate to `EventCoordinator`'s existing `UnhandledHandlerError`
routing (global rule: 任何未捕捉例外應轉入安全暫停).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BarClosed,
    Event,
    FillReceived,
    ManualPositionSyncCompleted,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
    PositionDiscrepancyDetected,
)
from tfx_quant.application.order_management.errors import (
    ActiveWorkflowInProgressError,
    OrderExposureExceededError,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind
from tfx_quant.domain.strategy_signal_engine import (
    EngineConfig,
    PositionSide,
    StrategyDecision,
    StrategySignalEngine,
)
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_info, log_warning

_logger = get_logger(__name__)

_DEFAULT_CLOCK_INTERVAL_SECONDS = 5.0

_EngineKey = tuple[Instrument, ContractMonth]

SelectedAccount = Callable[[], "TradingAccount | None"]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as `application.
    order_management.order_manager.EventBus`."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


@dataclass(frozen=True, slots=True)
class _OrderShape:
    side: Side
    quantity: int
    kind: OrderKind


def _order_shape_for(decision: StrategyDecision) -> _OrderShape:
    if decision.signal_kind is SignalKind.ENTER_LONG:
        return _OrderShape(Side.BUY, 1, OrderKind.OPEN)
    if decision.signal_kind is SignalKind.ENTER_SHORT:
        return _OrderShape(Side.SELL, 1, OrderKind.OPEN)
    if decision.signal_kind is SignalKind.ADD_LONG:
        return _OrderShape(Side.BUY, 1, OrderKind.OPEN)
    if decision.signal_kind is SignalKind.ADD_SHORT:
        return _OrderShape(Side.SELL, 1, OrderKind.OPEN)
    if decision.signal_kind is SignalKind.EXIT_ALL:
        closing_side = Side.SELL if decision.position_side is PositionSide.LONG else Side.BUY
        return _OrderShape(closing_side, decision.position_lots, OrderKind.CLOSE)
    raise ValueError(f"unsupported signal_kind for order submission: {decision.signal_kind}")


class StrategySignalEngineService:
    """Implements `application.ports.bar_signal_state.BarSignalStateStore`."""

    def __init__(
        self,
        *,
        order_manager: OrderManager,
        order_repository: OrderRepository,
        clock: Clock,
        event_bus: EventBus,
        selected_account: SelectedAccount,
        engine_config: EngineConfig | None = None,
        clock_interval_seconds: float = _DEFAULT_CLOCK_INTERVAL_SECONDS,
    ) -> None:
        self._order_manager = order_manager
        self._order_repository = order_repository
        self._clock = clock
        self._event_bus = event_bus
        self._selected_account = selected_account
        self._engine_config = engine_config
        self._clock_interval_seconds = clock_interval_seconds
        self._lock = threading.RLock()
        self._engines: dict[_EngineKey, StrategySignalEngine] = {}
        self._stale: dict[_EngineKey, bool] = {}
        self._gapped: dict[_EngineKey, bool] = {}
        self._position_uncertain: dict[_EngineKey, bool] = {}
        self._timer: threading.Timer | None = None
        self._running = False

        event_bus.subscribe(BarClosed, self._on_bar_closed)
        event_bus.subscribe(FillReceived, self._on_fill)
        event_bus.subscribe(MarketDataFreshnessChanged, self._on_freshness_changed)
        event_bus.subscribe(MarketDataGapDetected, self._on_gap_detected)
        event_bus.subscribe(MarketDataGapCleared, self._on_gap_cleared)
        event_bus.subscribe(PositionDiscrepancyDetected, self._on_discrepancy_detected)
        event_bus.subscribe(ManualPositionSyncCompleted, self._on_manual_sync_completed)

    # -- BarSignalStateStore --------------------------------------------------------------

    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        key = (instrument, contract)
        with self._lock:
            self._engines.pop(key, None)
            self._stale.pop(key, None)
            self._gapped.pop(key, None)
            self._position_uncertain.pop(key, None)
        log_info(
            _logger,
            "strategy_signal_engine_state_cleared",
            instrument=instrument.value,
            contract=contract.code,
        )

    # -- Lifecycle: the 04:55 clock-tick trigger -------------------------------------------

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

    def on_clock_tick(self, now: Timestamp | None = None) -> None:
        """Public so tests can drive the EOD-flatten timer directly, without a real
        timer — same convention as `OrderManager.on_clock_tick`."""
        resolved_now = now if now is not None else self._clock.now()
        with self._lock:
            keys = list(self._engines.keys())
        for instrument, contract in keys:
            self._evaluate_clock_tick(instrument, contract, resolved_now)

    def _evaluate_clock_tick(
        self, instrument: Instrument, contract: ContractMonth, now: Timestamp
    ) -> None:
        with self._lock:
            engine = self._engines.get((instrument, contract))
            if engine is None:
                return
            has_active_order = self._has_active_order(instrument, contract)
            position_state_uncertain = self._position_uncertain.get((instrument, contract), False)
            decision = engine.on_clock_tick(
                now,
                has_active_order=has_active_order,
                position_state_uncertain=position_state_uncertain,
            )
        self._log_decision(decision, instrument, contract)
        self._act_on_decision(decision, instrument, contract)

    # -- Event handlers ---------------------------------------------------------------------

    def _on_bar_closed(self, event: BarClosed) -> None:
        key = (event.instrument, event.contract)
        with self._lock:
            engine = self._engines.get(key)
            if engine is None:
                engine = StrategySignalEngine(
                    instrument=event.instrument,
                    contract=event.contract,
                    config=self._engine_config,
                )
                self._engines[key] = engine
            data_reliable = not self._stale.get(key, False) and not self._gapped.get(key, False)
            has_active_order = self._has_active_order(event.instrument, event.contract)
            position_state_uncertain = self._position_uncertain.get(key, False)
            decision = engine.on_bar_closed(
                event.bar,
                data_reliable=data_reliable,
                has_active_order=has_active_order,
                position_state_uncertain=position_state_uncertain,
            )
        self._log_decision(decision, event.instrument, event.contract)
        self._act_on_decision(decision, event.instrument, event.contract)

    def _on_fill(self, event: FillReceived) -> None:
        fill = event.fill
        order = self._order_repository.find_by_client_order_id(fill.client_order_id)
        if order is None:
            return  # OrderManager itself already logs the unmatched case
        key = (order.instrument, order.contract)
        with self._lock:
            engine = self._engines.get(key)
            if engine is None:
                return
            engine.on_fill_confirmed(
                side=fill.side,
                price=fill.price.amount,
                quantity=fill.quantity.lots,
                at=fill.at,
            )
        log_info(
            _logger,
            "strategy_signal_engine_fill_applied",
            instrument=order.instrument.value,
            contract=order.contract.code,
            side=fill.side.value,
            price=str(fill.price.amount),
            quantity=fill.quantity.lots,
        )

    def _on_freshness_changed(self, event: MarketDataFreshnessChanged) -> None:
        with self._lock:
            self._stale[(event.instrument, event.contract)] = event.is_stale

    def _on_gap_detected(self, event: MarketDataGapDetected) -> None:
        with self._lock:
            self._gapped[(event.instrument, event.contract)] = True

    def _on_gap_cleared(self, event: MarketDataGapCleared) -> None:
        with self._lock:
            self._gapped[(event.instrument, event.contract)] = False

    def _on_discrepancy_detected(self, event: PositionDiscrepancyDetected) -> None:
        with self._lock:
            self._position_uncertain[(event.instrument, event.contract)] = True

    def _on_manual_sync_completed(self, event: ManualPositionSyncCompleted) -> None:
        with self._lock:
            self._position_uncertain[(event.instrument, event.contract)] = False

    # -- Decision -> order submission --------------------------------------------------------

    def _has_active_order(self, instrument: Instrument, contract: ContractMonth) -> bool:
        account = self._selected_account()
        if account is None:
            return True  # fail closed: no resolvable account means we cannot safely submit
        return bool(self._order_repository.find_active_for_contract(account, instrument, contract))

    def _act_on_decision(
        self, decision: StrategyDecision, instrument: Instrument, contract: ContractMonth
    ) -> None:
        if decision.signal_kind is None:
            return
        account = self._selected_account()
        if account is None:
            log_warning(
                _logger, "strategy_signal_no_account_selected", decision_id=decision.decision_id
            )
            return
        if decision.current_price is None or decision.intent_key is None:
            log_warning(
                _logger,
                "strategy_signal_decision_missing_submission_fields",
                decision_id=decision.decision_id,
            )
            return
        shape = _order_shape_for(decision)
        request = OrderRequest(
            account=account,
            instrument=instrument,
            contract=contract,
            side=shape.side,
            quantity=Quantity(shape.quantity),
            price=Price(decision.current_price),
            kind=shape.kind,
            time_in_force=TimeInForce.ROD,
            idempotency_key=decision.intent_key,
            workflow_id=decision.intent_key,
            reason=decision.reason,
        )
        try:
            self._order_manager.submit(request)
        except (ActiveWorkflowInProgressError, OrderExposureExceededError) as exc:
            log_warning(
                _logger,
                "strategy_signal_order_submit_rejected",
                decision_id=decision.decision_id,
                rule=decision.rule,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    def _log_decision(
        self, decision: StrategyDecision, instrument: Instrument, contract: ContractMonth
    ) -> None:
        log_info(
            _logger,
            "strategy_signal_evaluated",
            instrument=instrument.value,
            contract=contract.code,
            decision_id=decision.decision_id,
            intent_key=decision.intent_key,
            rule=decision.rule,
            trigger=decision.trigger,
            passed=decision.passed,
            signal_kind=decision.signal_kind.value if decision.signal_kind else None,
            reason=decision.reason,
            position_side=decision.position_side.value,
            position_lots=decision.position_lots,
            ma_value=str(decision.ma_value) if decision.ma_value is not None else None,
            ma_slope=decision.ma_slope.value,
            ma_is_choppy=decision.ma_is_choppy,
            entry_gate_open=decision.entry_gate_open,
            stop_basis=str(decision.stop_basis) if decision.stop_basis is not None else None,
            current_favorable_points=(
                str(decision.current_favorable_points)
                if decision.current_favorable_points is not None
                else None
            ),
            max_favorable_points=(
                str(decision.max_favorable_points)
                if decision.max_favorable_points is not None
                else None
            ),
        )


__all__ = ["StrategySignalEngineService"]
