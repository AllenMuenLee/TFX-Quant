"""Pure validation rules for `application.risk.risk_supervisor.RiskSupervisor` — no I/O,
no state. Same shape as `application.reversal_scaling.gates`: callers gather
already-fetched data first; these functions return `None` (ok) or a Chinese reason
string suitable for showing directly to the operator, and log every evaluation
regardless of outcome.
"""

from __future__ import annotations

from datetime import time

from tfx_quant.domain.risk import (
    ENTRY_GATE_LOCAL_TIME,
    EOD_FLATTEN_LOCAL_TIME,
    is_within_no_entry_window,
)
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)


def validate_entry_window(
    now: Timestamp,
    *,
    eod_flatten_local_time: time = EOD_FLATTEN_LOCAL_TIME,
    entry_gate_local_time: time = ENTRY_GATE_LOCAL_TIME,
) -> str | None:
    """The independent, strategy-engine-agnostic half of "08:45、09:45 不允許建倉／加碼；
    最早 10:45 才能建立日盤策略部位" — callers submitting an `OPEN`-kind order (new entry
    or add-on) must consult this before ever calling `OrderManager.submit()`. Closing/
    risk-driven orders are never subject to this gate ("平倉風險動作不受建倉禁令阻擋") —
    callers simply never call this for a close."""
    within_band = is_within_no_entry_window(
        now,
        eod_flatten_local_time=eod_flatten_local_time,
        entry_gate_local_time=entry_gate_local_time,
    )
    reason = (
        f"位於 {eod_flatten_local_time.isoformat(timespec='minutes')}-"
        f"{entry_gate_local_time.isoformat(timespec='minutes')} 禁入時段，禁止建倉／加碼"
        if within_band
        else None
    )
    log_info(
        _logger,
        "risk_entry_window_evaluated",
        now=now.value.isoformat(),
        within_no_entry_window=within_band,
        passed=reason is None,
        reason=reason,
    )
    return reason


__all__ = ["validate_entry_window"]
