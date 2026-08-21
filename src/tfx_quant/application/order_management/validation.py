"""Pure validation rules for `OrderManager.submit`. No I/O, no state — same shape as
`application.instrument_selection.validation`: takes already-fetched data, returns
`None` (ok) or a Chinese reason string suitable for showing directly to the operator.
"""

from __future__ import annotations

from collections.abc import Sequence

from tfx_quant.domain.order_state_machine import OrderIntent, worst_case_net_position_range
from tfx_quant.domain.quantity import MAX_LOTS, NetPosition
from tfx_quant.domain.side import Side
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)


def validate_exposure_within_cap(
    current: NetPosition,
    active: Sequence[OrderIntent],
    *,
    candidate_side: Side,
    candidate_lots: int,
) -> str | None:
    """Rejects a candidate order whenever the worst-case net position it could produce
    (current position plus every possible fill outcome of every still-active order,
    including the candidate itself) would exceed `MAX_LOTS` in either direction — the
    "使用送單前持倉加上所有可能成交的活動委託計算最壞曝險" requirement. Returns `None`
    when the candidate is allowed."""
    combined_lo, combined_hi = _fold_candidate(current, active, candidate_side, candidate_lots)
    if combined_lo < -MAX_LOTS or combined_hi > MAX_LOTS:
        reason = (
            f"送單前持倉 {current.lots} 口加計所有可能成交的活動委託與本筆委託後，"
            f"最壞曝險範圍為 [{combined_lo}, {combined_hi}]，超過最大 {MAX_LOTS} 口限制，禁止送單"
        )
    else:
        reason = None
    log_info(
        _logger,
        "order_exposure_validated",
        current_net=current.lots,
        candidate_side=candidate_side.value,
        candidate_lots=candidate_lots,
        worst_case_low=combined_lo,
        worst_case_high=combined_hi,
        passed=reason is None,
        reason=reason,
    )
    return reason


def _fold_candidate(
    current: NetPosition, active: Sequence[OrderIntent], candidate_side: Side, candidate_lots: int
) -> tuple[int, int]:
    lo, hi = worst_case_net_position_range(current, active)
    signed = candidate_lots if candidate_side is Side.BUY else -candidate_lots
    if signed > 0:
        hi += signed
    else:
        lo += signed
    return lo, hi


__all__ = ["validate_exposure_within_cap"]
