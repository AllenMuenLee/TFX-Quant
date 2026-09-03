"""Pure validation rules shared by the switch workflow, the startup safety checklist,
and (later) Feature 06's order submission path. No I/O, no state — every function here
takes already-fetched data and returns either `None` (ok) or a Chinese reason string
suitable for showing directly to the operator.
"""

from __future__ import annotations

from collections.abc import Sequence

from tfx_quant.application.instrument_selection.selection import ResolvedSelection
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import Order
from tfx_quant.domain.position import Position
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_info
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)


def validate_can_open(entry: InstrumentMasterEntry | None, *, as_of: Timestamp) -> str | None:
    """Feature 03's acceptance criteria: 到期、停止交易、主檔缺漏、價格跳動單位不符時禁止
    建倉並顯示原因 — this covers the first three; `validate_order_price` covers the
    fourth (it needs a candidate price, which this function's callers may not have
    yet). Returns `None` when opening a position is allowed."""
    if entry is None:
        reason = "商品主檔缺漏，禁止建倉"
    elif not entry.tradable:
        reason = f"{entry.vendor_symbol} 已停止交易，禁止建倉"
    elif entry.expiry_date < as_of.value.date():
        reason = f"{entry.vendor_symbol} 契約已到期（{entry.expiry_date}），禁止建倉"
    else:
        reason = None
    log_info(
        _logger,
        "instrument_master_entry_validated",
        rule="tradable_and_not_expired",
        vendor_symbol=entry.vendor_symbol if entry is not None else None,
        entry_present=entry is not None,
        passed=reason is None,
        reason=reason,
    )
    return reason


def validate_order_price(entry: InstrumentMasterEntry, price: Price) -> str | None:
    """Rejects a price that isn't an exact multiple of the contract's tick size."""
    if price.amount % entry.tick_size != 0:
        reason = (
            f"價格 {price.amount} 不符合 {entry.vendor_symbol} 的最小跳動單位 "
            f"{entry.tick_size}，禁止建倉"
        )
    else:
        reason = None
    log_info(
        _logger,
        "instrument_master_entry_validated",
        rule="tick_size",
        vendor_symbol=entry.vendor_symbol,
        tick_size=str(entry.tick_size),
        passed=reason is None,
        reason=reason,
    )
    return reason


def check_quote_position_order_consistent(
    *,
    current: ResolvedSelection | None,
    positions: Sequence[Position],
    open_orders: Sequence[Order],
) -> bool:
    """Feature 03's acceptance criteria: 行情、持倉、委託三者不一致時不得啟動.

    True only when every non-flat position and every open order identifies the same
    instrument/contract as the currently-subscribed quote (`current`). `current` being
    `None` (nothing selected/confirmed yet) is never consistent."""
    if current is None:
        log_info(
            _logger,
            "quote_position_order_consistency_checked",
            passed=False,
            reason="no confirmed instrument/contract selection",
        )
        return False
    expected = (current.instrument, current.contract)
    mismatched_positions = [
        {
            "account_no": mask_account(p.account.account_no),
            "instrument": p.instrument.value,
            "contract": p.contract.code,
        }
        for p in positions
        if not p.net.is_flat and (p.instrument, p.contract) != expected
    ]
    mismatched_orders = [
        {
            "account_no": mask_account(o.account.account_no),
            "instrument": o.instrument.value,
            "contract": o.contract.code,
        }
        for o in open_orders
        if (o.instrument, o.contract) != expected
    ]
    consistent = not mismatched_positions and not mismatched_orders
    log_info(
        _logger,
        "quote_position_order_consistency_checked",
        passed=consistent,
        quote_instrument=current.instrument.value,
        quote_contract=current.contract.code,
        mismatched_positions=mismatched_positions,
        mismatched_orders=mismatched_orders,
    )
    return consistent
