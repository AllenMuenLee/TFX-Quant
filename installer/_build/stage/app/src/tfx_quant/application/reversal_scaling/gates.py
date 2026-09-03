"""Pure validation rules for reversal/scaling — no I/O, no state. Same convention as
`application/instrument_selection/validation.py` and `application/order_management/
validation.py`: callers gather already-fetched data first; these functions return a
structured result or a Chinese reason string suitable for showing directly to the
operator, and log every evaluation regardless of outcome.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import time, timedelta

from tfx_quant.application.order_management.validation import validate_exposure_within_cap
from tfx_quant.application.settings.trading_settings import REQUIRED_EOD_FLATTEN_TIME
from tfx_quant.domain.order_state_machine import OrderIntent
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reversal_workflow import FlatConfirmationResult
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)


def evaluate_flat_confirmation(
    *,
    position: Position | None,
    active_orders: Sequence[OrderIntent],
    session_healthy: bool,
    market_data_healthy: bool,
) -> FlatConfirmationResult:
    """`position` is `None` when the broker's position query has no row at all for this
    (account, instrument, contract) — treated as flat (0 lots), same as an explicit
    zero-net row. `active_orders` is expected pre-scoped to this contract (e.g. via
    `OrderRepository.find_active_for_contract`) but is defensively re-checked against
    `OrderIntent.is_active` regardless."""
    position_lots = 0 if position is None else position.net.lots
    has_active_or_unknown = any(order.is_active for order in active_orders)
    result = FlatConfirmationResult(
        is_flat=position_lots == 0,
        position_lots=position_lots,
        has_active_or_unknown_orders=has_active_or_unknown,
        session_healthy=session_healthy,
        market_data_healthy=market_data_healthy,
    )
    log_info(
        _logger,
        "flat_confirmation_gate_evaluated",
        is_flat=result.is_flat,
        position_lots=result.position_lots,
        has_active_or_unknown_orders=result.has_active_or_unknown_orders,
        session_healthy=result.session_healthy,
        market_data_healthy=result.market_data_healthy,
        passed=result.is_confirmed,
    )
    return result


def evaluate_scaling_gate(
    *, current_net: NetPosition, active_orders: Sequence[OrderIntent], signal_kind: SignalKind
) -> str | None:
    """Only ever allows a single 1-lot add-on from an exact ±1 lot position to ±2 —
    "只有實際持倉精確為 ±1、無活動／未知委託、訊號方向相同且風險閘門允許時，才送 1 口加碼"."""
    blocking_orders = [order for order in active_orders if order.is_active]
    reason: str | None
    if signal_kind not in (SignalKind.ADD_LONG, SignalKind.ADD_SHORT):
        reason = f"訊號種類 {signal_kind.value} 不適用於加碼"
    elif abs(current_net.lots) != 1:
        reason = f"目前持倉 {current_net.lots} 口，非精確 ±1 口，禁止加碼"
    elif blocking_orders:
        reason = "存在活動或狀態不明委託，禁止加碼"
    elif signal_kind is SignalKind.ADD_LONG and current_net.lots != 1:
        reason = "訊號方向（做多）與目前持倉方向不符，禁止加碼"
    elif signal_kind is SignalKind.ADD_SHORT and current_net.lots != -1:
        reason = "訊號方向（做空）與目前持倉方向不符，禁止加碼"
    else:
        candidate_side = Side.BUY if signal_kind is SignalKind.ADD_LONG else Side.SELL
        reason = validate_exposure_within_cap(
            current_net, active_orders, candidate_side=candidate_side, candidate_lots=1
        )
    log_info(
        _logger,
        "scaling_gate_evaluated",
        signal_kind=signal_kind.value,
        current_net=current_net.lots,
        active_order_count=len(blocking_orders),
        passed=reason is None,
        reason=reason,
    )
    return reason


def _time_of_day(t: time) -> timedelta:
    return timedelta(hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond)


def is_too_close_to_eod(now: Timestamp, *, margin: timedelta) -> bool:
    """A coarse, date-agnostic time-of-day check — never trading-day/session-boundary
    aware (unlike `domain.trading_calendar`), since this only needs to refuse *starting*
    a new multi-step workflow shortly before the mandatory 04:55 flatten deadline, not
    implement the deadline itself (that's Feature 10's job — see
    `docs/adr/0009-safe-reversal-and-scaling.md`)."""
    current = _time_of_day(now.value.time())
    cutoff = _time_of_day(REQUIRED_EOD_FLATTEN_TIME)
    return cutoff - margin <= current < cutoff


__all__ = ["evaluate_flat_confirmation", "evaluate_scaling_gate", "is_too_close_to_eod"]
