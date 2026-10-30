"""Order/fill blotter rows — Feature 12's 委託／成交 group."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.domain.order import OrderKind
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.side import Side
from tfx_quant.telemetry.masking import mask_account

_DIRECTION = {Side.BUY: "多", Side.SELL: "空"}
_EFFECT = {OrderKind.OPEN: "開", OrderKind.CLOSE: "平"}
_STATUS_ZH = {
    OrderStatus.CREATED: "已建立",
    OrderStatus.SUBMITTING: "送出中",
    OrderStatus.ACKNOWLEDGED: "已受理",
    OrderStatus.PARTIALLY_FILLED: "部分成交",
    OrderStatus.FILLED: "全部成交",
    OrderStatus.CANCEL_PENDING: "取消中",
    OrderStatus.CANCELLED: "已取消",
    OrderStatus.REJECTED: "已拒絕",
    OrderStatus.UNKNOWN: "狀態不明",
}
_NEEDS_ATTENTION = {OrderStatus.REJECTED, OrderStatus.UNKNOWN, OrderStatus.PARTIALLY_FILLED}


@dataclass(frozen=True, slots=True)
class OrderRow:
    local_order_id: str
    broker_order_no: str | None
    instrument_contract: str
    direction: str
    effect: str
    lots: int
    cumulative_filled: int
    requested_price: str
    average_fill_price: str
    time_in_force: str
    status: str
    triggered_at: str
    created_at: str
    updated_at: str
    reason: str
    status_detail: str
    needs_attention: bool


def build_orders_view(order_repository: OrderRepository) -> tuple[OrderRow, ...]:
    return tuple(
        OrderRow(
            local_order_id=str(intent.local_order_id.value),
            broker_order_no=(
                None if intent.broker_order_no is None else mask_account(intent.broker_order_no)
            ),
            instrument_contract=f"{intent.instrument.value} / {intent.contract.code}",
            direction=_DIRECTION[intent.side],
            effect=_EFFECT.get(intent.kind, "?"),
            lots=intent.quantity.lots,
            cumulative_filled=intent.filled_quantity,
            requested_price=(
                "—" if intent.requested_price is None else str(intent.requested_price.amount)
            ),
            average_fill_price=(
                "—" if intent.avg_fill_price is None else str(intent.avg_fill_price.amount)
            ),
            time_in_force=("—" if intent.time_in_force is None else intent.time_in_force.value),
            status=_STATUS_ZH.get(intent.status, intent.status.value),
            triggered_at=(
                "—"
                if intent.triggered_at is None
                else intent.triggered_at.value.strftime("%Y-%m-%d %H:%M:%S")
            ),
            created_at=intent.created_at.value.strftime("%Y-%m-%d %H:%M:%S"),
            updated_at=intent.updated_at.value.strftime("%Y-%m-%d %H:%M:%S"),
            reason=intent.reason or "舊委託未保存原因",
            status_detail=intent.reject_reason or intent.last_event_summary or "—",
            needs_attention=intent.status in _NEEDS_ATTENTION,
        )
        for intent in order_repository.list_all()
    )


__all__ = ["OrderRow", "build_orders_view"]
