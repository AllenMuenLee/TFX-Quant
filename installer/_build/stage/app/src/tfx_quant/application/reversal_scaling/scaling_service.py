"""ScalingService — the ±1 -> ±2 add-on ("加碼") evaluator/submitter.

Much smaller than `ReversalWorkflowService`: single-step, no persisted workflow of its
own. `OrderManager`'s own idempotency-key dedup already guarantees "不得因後續 K 棒重送"
(a duplicate `evaluate_and_submit` call with the same `idempotency_key` never produces a
second order), and its own partial-fill/reject/timeout/unknown handling already produces
`OrderRequiresManualReview` with zero extra code needed here — see
`docs/adr/0009-safe-reversal-and-scaling.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.application.reversal_scaling.errors import InvalidSignalKindError
from tfx_quant.application.reversal_scaling.gates import evaluate_scaling_gate, is_too_close_to_eod
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderIntent
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind, StrategySignal
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)
_DEFAULT_EOD_MARGIN = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class ScalingDecision:
    allowed: bool
    reason: str | None
    order_intent: OrderIntent | None


class ScalingService:
    def __init__(
        self,
        *,
        order_manager: OrderManager,
        order_repository: OrderRepository,
        trade_gateway: TradeGatewayPort,
        clock: Clock,
        eod_margin: timedelta = _DEFAULT_EOD_MARGIN,
    ) -> None:
        self._order_manager = order_manager
        self._order_repository = order_repository
        self._trade_gateway = trade_gateway
        self._clock = clock
        self._eod_margin = eod_margin

    def evaluate_and_submit(
        self,
        signal: StrategySignal,
        *,
        account: TradingAccount,
        price: Price,
        idempotency_key: str,
    ) -> ScalingDecision:
        if signal.kind not in (SignalKind.ADD_LONG, SignalKind.ADD_SHORT):
            raise InvalidSignalKindError(
                f"ScalingService only handles ADD_LONG/ADD_SHORT signals, got {signal.kind.value}"
            )

        existing = self._order_repository.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            # Same idempotency key as an earlier call (a retry, a repeated K-bar-driven
            # signal) — dedupe *before* re-running the gate, exactly like `OrderManager.
            # submit()` does. Re-running the gate first would reject this as "an active
            # order already exists" (that active order being this very same, already-
            # submitted scale order), which would be the right outcome (no second order)
            # for the wrong stated reason.
            log_info(
                _logger,
                "scaling_submit_deduped",
                idempotency_key=idempotency_key,
                local_order_id=str(existing.local_order_id.value),
                status=existing.status.value,
            )
            return ScalingDecision(allowed=True, reason=None, order_intent=existing)

        now = self._clock.now()
        if is_too_close_to_eod(now, margin=self._eod_margin):
            reason = "過於接近 04:55 收盤平倉時間，不加碼"
            log_info(
                _logger,
                "scaling_evaluated",
                signal_kind=signal.kind.value,
                instrument=signal.instrument.value,
                contract=signal.contract.code,
                trigger_reason=signal.reason,
                allowed=False,
                reason=reason,
            )
            return ScalingDecision(allowed=False, reason=reason, order_intent=None)

        current_net = self._current_net(account, signal.instrument, signal.contract)
        active_orders = self._order_repository.find_active_for_contract(
            account, signal.instrument, signal.contract
        )
        gate_reason = evaluate_scaling_gate(
            current_net=current_net, active_orders=active_orders, signal_kind=signal.kind
        )
        log_info(
            _logger,
            "scaling_evaluated",
            signal_kind=signal.kind.value,
            instrument=signal.instrument.value,
            contract=signal.contract.code,
            current_net=current_net.lots,
            active_order_count=len(active_orders),
            trigger_reason=signal.reason,
            allowed=gate_reason is None,
            reason=gate_reason,
        )
        if gate_reason is not None:
            return ScalingDecision(allowed=False, reason=gate_reason, order_intent=None)

        side = Side.BUY if signal.kind is SignalKind.ADD_LONG else Side.SELL
        request = OrderRequest(
            account=account,
            instrument=signal.instrument,
            contract=signal.contract,
            side=side,
            quantity=Quantity(1),
            price=price,
            kind=OrderKind.OPEN,
            time_in_force=TimeInForce.ROD,
            idempotency_key=idempotency_key,
            workflow_id=idempotency_key,
            reason=signal.reason,
        )
        order_intent = self._order_manager.submit(request)
        return ScalingDecision(allowed=True, reason=None, order_intent=order_intent)

    def _current_net(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> NetPosition:
        for position in self._trade_gateway.query_positions():
            if (
                position.account == account
                and position.instrument == instrument
                and position.contract == contract
            ):
                return position.net
        return NetPosition(0)


__all__ = ["ScalingDecision", "ScalingService"]
