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


def validate_can_open(entry: InstrumentMasterEntry | None, *, as_of: Timestamp) -> str | None:
    """Feature 03's acceptance criteria: 到期、停止交易、主檔缺漏、價格跳動單位不符時禁止
    建倉並顯示原因 — this covers the first three; `validate_order_price` covers the
    fourth (it needs a candidate price, which this function's callers may not have
    yet). Returns `None` when opening a position is allowed."""
    if entry is None:
        return "商品主檔缺漏，禁止建倉"
    if not entry.tradable:
        return f"{entry.vendor_symbol} 已停止交易，禁止建倉"
    if entry.expiry_date < as_of.value.date():
        return f"{entry.vendor_symbol} 契約已到期（{entry.expiry_date}），禁止建倉"
    return None


def validate_order_price(entry: InstrumentMasterEntry, price: Price) -> str | None:
    """Rejects a price that isn't an exact multiple of the contract's tick size."""
    if price.amount % entry.tick_size != 0:
        return (
            f"價格 {price.amount} 不符合 {entry.vendor_symbol} 的最小跳動單位 "
            f"{entry.tick_size}，禁止建倉"
        )
    return None


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
        return False
    expected = (current.instrument, current.contract)
    for position in positions:
        if not position.net.is_flat and (position.instrument, position.contract) != expected:
            return False
    return all((order.instrument, order.contract) == expected for order in open_orders)
