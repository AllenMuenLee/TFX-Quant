from __future__ import annotations

from decimal import Decimal

import pytest

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import IllegalStateTransitionError, InvalidOrderError
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind
from tfx_quant.domain.order_state_machine import (
    LocalOrderId,
    OrderIntent,
    OrderReport,
    OrderStateMachine,
    OrderStatus,
    worst_case_net_position_range,
)
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
CONTRACT = ContractMonth(year=2026, month=9)
NOW = Timestamp.now()


def _intent(
    *,
    status: OrderStatus = OrderStatus.CREATED,
    side: Side = Side.BUY,
    quantity: Quantity | None = None,
    filled_quantity: int = 0,
    avg_fill_price: Price | None = None,
    last_applied_broker_seq_no: int | None = None,
) -> OrderIntent:
    return OrderIntent(
        local_order_id=LocalOrderId(),
        client_order_id=ClientOrderId(),
        workflow_id="wf-1",
        idempotency_key="key-1",
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=CONTRACT,
        side=side,
        kind=OrderKind.OPEN,
        quantity=quantity if quantity is not None else Quantity(1),
        status=status,
        created_at=NOW,
        updated_at=NOW,
        filled_quantity=filled_quantity,
        avg_fill_price=avg_fill_price,
        last_applied_broker_seq_no=last_applied_broker_seq_no,
    )


def _fill(client_order_id: ClientOrderId, *, qty: int, price: str, fill_no: str, seq: int) -> Fill:
    return Fill(
        client_order_id=client_order_id,
        instrument=Instrument.MXF,
        side=Side.BUY,
        quantity=Quantity(qty),
        price=Price(Decimal(price)),
        at=NOW,
        broker_fill_no=fill_no,
        broker_seq_no=seq,
    )


# -- OrderIntent invariants ---------------------------------------------------------


def test_intent_rejects_filled_quantity_above_requested() -> None:
    with pytest.raises(InvalidOrderError):
        _intent(filled_quantity=2, avg_fill_price=Price(Decimal("100")))


def test_intent_rejects_avg_price_when_unfilled() -> None:
    with pytest.raises(InvalidOrderError):
        _intent(filled_quantity=0, avg_fill_price=Price(Decimal("100")))


def test_intent_rejects_missing_avg_price_when_filled() -> None:
    with pytest.raises(InvalidOrderError):
        _intent(filled_quantity=1, avg_fill_price=None)


# -- Basic transitions ---------------------------------------------------------------


def test_created_transitions_to_submitting() -> None:
    machine = OrderStateMachine(_intent(status=OrderStatus.CREATED))
    record = machine.mark_submitting(at=NOW)
    assert record.status is OrderStatus.SUBMITTING


def test_submitting_to_acknowledged_via_order_report() -> None:
    intent = _intent(status=OrderStatus.SUBMITTING)
    machine = OrderStateMachine(intent)
    report = OrderReport(
        client_order_id=intent.client_order_id,
        status=OrderStatus.ACKNOWLEDGED,
        broker_seq_no=1,
        at=NOW,
        broker_order_no="B001",
    )
    record, applied = machine.apply_order_report(report)
    assert applied is True
    assert record.status is OrderStatus.ACKNOWLEDGED
    assert record.broker_order_no == "B001"
    assert record.last_applied_broker_seq_no == 1


def test_illegal_order_report_transition_raises() -> None:
    intent = _intent(status=OrderStatus.CREATED)
    machine = OrderStateMachine(intent)
    report = OrderReport(
        client_order_id=intent.client_order_id, status=OrderStatus.FILLED, broker_seq_no=1, at=NOW
    )
    with pytest.raises(IllegalStateTransitionError):
        machine.apply_order_report(report)


def test_reject_reason_recorded() -> None:
    intent = _intent(status=OrderStatus.SUBMITTING)
    machine = OrderStateMachine(intent)
    report = OrderReport(
        client_order_id=intent.client_order_id,
        status=OrderStatus.REJECTED,
        broker_seq_no=1,
        at=NOW,
        reject_reason="資金不足",
    )
    record, applied = machine.apply_order_report(report)
    assert applied is True
    assert record.status is OrderStatus.REJECTED
    assert record.reject_reason == "資金不足"


# -- Duplicate / out-of-order dedup ---------------------------------------------------


def test_duplicate_order_report_seq_ignored() -> None:
    intent = _intent(status=OrderStatus.SUBMITTING, last_applied_broker_seq_no=1)
    machine = OrderStateMachine(intent)
    report = OrderReport(
        client_order_id=intent.client_order_id,
        status=OrderStatus.ACKNOWLEDGED,
        broker_seq_no=1,
        at=NOW,
    )
    record, applied = machine.apply_order_report(report)
    assert applied is False
    assert record.status is OrderStatus.SUBMITTING  # unchanged


def test_out_of_order_order_report_ignored() -> None:
    intent = _intent(status=OrderStatus.ACKNOWLEDGED, last_applied_broker_seq_no=5)
    machine = OrderStateMachine(intent)
    stale_report = OrderReport(
        client_order_id=intent.client_order_id,
        status=OrderStatus.CANCELLED,
        broker_seq_no=3,
        at=NOW,
    )
    record, applied = machine.apply_order_report(stale_report)
    assert applied is False
    assert record.status is OrderStatus.ACKNOWLEDGED


def test_duplicate_fill_seq_ignored() -> None:
    intent = _intent(
        status=OrderStatus.ACKNOWLEDGED, quantity=Quantity(2), last_applied_broker_seq_no=1
    )
    machine = OrderStateMachine(intent)
    fill = _fill(intent.client_order_id, qty=1, price="18500", fill_no="F1", seq=1)
    record, applied = machine.apply_fill(fill, broker_seq_no=1)
    assert applied is False
    assert record.filled_quantity == 0


# -- Partial / full fills --------------------------------------------------------------


def test_partial_then_full_fill_accumulates_and_averages_price() -> None:
    intent = _intent(status=OrderStatus.ACKNOWLEDGED, quantity=Quantity(2))
    machine = OrderStateMachine(intent)

    fill1 = _fill(intent.client_order_id, qty=1, price="18500", fill_no="F1", seq=1)
    record, applied = machine.apply_fill(fill1, broker_seq_no=1)
    assert applied is True
    assert record.status is OrderStatus.PARTIALLY_FILLED
    assert record.filled_quantity == 1
    assert record.avg_fill_price == Price(Decimal("18500"))

    fill2 = _fill(intent.client_order_id, qty=1, price="18600", fill_no="F2", seq=2)
    record, applied = machine.apply_fill(fill2, broker_seq_no=2)
    assert applied is True
    assert record.status is OrderStatus.FILLED
    assert record.filled_quantity == 2
    assert record.avg_fill_price == Price(Decimal("18550"))


def test_overfill_is_rejected_not_applied() -> None:
    intent = _intent(status=OrderStatus.ACKNOWLEDGED, quantity=Quantity(1))
    machine = OrderStateMachine(intent)
    fill = _fill(intent.client_order_id, qty=2, price="18500", fill_no="F1", seq=1)
    record, applied = machine.apply_fill(fill, broker_seq_no=1)
    assert applied is False
    assert record.filled_quantity == 0


def test_fill_after_terminal_status_is_rejected() -> None:
    intent = _intent(
        status=OrderStatus.FILLED,
        quantity=Quantity(1),
        filled_quantity=1,
        avg_fill_price=Price(Decimal("18500")),
        last_applied_broker_seq_no=1,
    )
    machine = OrderStateMachine(intent)
    fill = _fill(intent.client_order_id, qty=1, price="18500", fill_no="F2", seq=2)
    record, applied = machine.apply_fill(fill, broker_seq_no=2)
    assert applied is False
    assert record.status is OrderStatus.FILLED


# -- UNKNOWN: blocks control flow, not fact recording ----------------------------------


def test_mark_unknown_from_active_status() -> None:
    machine = OrderStateMachine(_intent(status=OrderStatus.ACKNOWLEDGED))
    record = machine.mark_unknown(at=NOW, reason="timeout")
    assert record.status is OrderStatus.UNKNOWN


def test_mark_unknown_from_terminal_status_raises() -> None:
    intent = _intent(
        status=OrderStatus.FILLED,
        filled_quantity=1,
        avg_fill_price=Price(Decimal("18500")),
    )
    machine = OrderStateMachine(intent)
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_unknown(at=NOW, reason="whatever")


def test_late_fill_applies_from_unknown() -> None:
    intent = _intent(status=OrderStatus.UNKNOWN, quantity=Quantity(1), last_applied_broker_seq_no=1)
    machine = OrderStateMachine(intent)
    fill = _fill(intent.client_order_id, qty=1, price="18500", fill_no="F2", seq=2)
    record, applied = machine.apply_fill(fill, broker_seq_no=2)
    assert applied is True
    assert record.status is OrderStatus.FILLED


# -- Cancel path / race ------------------------------------------------------------------


def test_mark_cancel_pending_requires_acknowledged_or_partially_filled() -> None:
    machine = OrderStateMachine(_intent(status=OrderStatus.CREATED))
    with pytest.raises(IllegalStateTransitionError):
        machine.mark_cancel_pending(at=NOW)


def test_cancel_pending_then_fill_race_resolves_to_filled() -> None:
    intent = _intent(status=OrderStatus.ACKNOWLEDGED, quantity=Quantity(1))
    machine = OrderStateMachine(intent)
    machine.mark_cancel_pending(at=NOW)
    fill = _fill(intent.client_order_id, qty=1, price="18500", fill_no="F1", seq=1)
    record, applied = machine.apply_fill(fill, broker_seq_no=1)
    assert applied is True
    assert record.status is OrderStatus.FILLED


def test_cancel_pending_then_cancel_confirmed() -> None:
    intent = _intent(status=OrderStatus.ACKNOWLEDGED)
    machine = OrderStateMachine(intent)
    machine.mark_cancel_pending(at=NOW)
    report = OrderReport(
        client_order_id=intent.client_order_id,
        status=OrderStatus.CANCELLED,
        broker_seq_no=1,
        at=NOW,
    )
    record, applied = machine.apply_order_report(report)
    assert applied is True
    assert record.status is OrderStatus.CANCELLED


# -- worst_case_net_position_range ------------------------------------------------------


def test_worst_case_range_flat_no_active_orders() -> None:
    assert worst_case_net_position_range(NetPosition(0), ()) == (0, 0)


def test_worst_case_range_long_position_with_active_buy_order() -> None:
    active = [_intent(status=OrderStatus.ACKNOWLEDGED, side=Side.BUY, quantity=Quantity(1))]
    assert worst_case_net_position_range(NetPosition(1), active) == (1, 2)


def test_worst_case_range_short_position_with_active_sell_order() -> None:
    active = [_intent(status=OrderStatus.ACKNOWLEDGED, side=Side.SELL, quantity=Quantity(1))]
    assert worst_case_net_position_range(NetPosition(-1), active) == (-2, -1)


def test_worst_case_range_ignores_terminal_orders() -> None:
    active = [
        _intent(
            status=OrderStatus.FILLED,
            side=Side.BUY,
            quantity=Quantity(1),
            filled_quantity=1,
            avg_fill_price=Price(Decimal("18500")),
        )
    ]
    assert worst_case_net_position_range(NetPosition(1), active) == (1, 1)


def test_worst_case_range_ignores_fully_filled_remaining_zero() -> None:
    active = [
        _intent(
            status=OrderStatus.PARTIALLY_FILLED,
            side=Side.BUY,
            quantity=Quantity(1),
            filled_quantity=1,
            avg_fill_price=Price(Decimal("18500")),
        )
    ]
    # filled_quantity == quantity.lots would violate the invariant above 0<f<=lots plus
    # status PARTIALLY_FILLED with remaining 0 is a degenerate case that should still
    # contribute nothing to the range.
    assert worst_case_net_position_range(NetPosition(1), active) == (1, 1)
