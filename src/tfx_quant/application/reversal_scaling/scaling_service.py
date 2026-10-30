"""Compatibility boundary for the retired add-on ("加碼") workflow.

The active strategy always opens two lots in one order. Every call is therefore rejected
so an old or accidentally retained caller cannot increase an existing position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.application.reversal_scaling.errors import InvalidSignalKindError
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.money import Price
from tfx_quant.domain.order_state_machine import OrderIntent
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

        reason = "策略固定單次進場 2 口，已永久停用追加部位"
        log_info(
            _logger,
            "scaling_disabled",
            signal_kind=signal.kind.value,
            instrument=signal.instrument.value,
            contract=signal.contract.code,
            trigger_reason=signal.reason,
            account=str(account),
            requested_price=str(price.amount),
            idempotency_key=idempotency_key,
            allowed=False,
            reason=reason,
        )
        return ScalingDecision(allowed=False, reason=reason, order_intent=None)


__all__ = ["ScalingDecision", "ScalingService"]
