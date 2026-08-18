from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidPositionError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp

ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
CONTRACT = ContractMonth(year=2026, month=9)


def test_flat_position_requires_no_average_price() -> None:
    Position(
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=CONTRACT,
        net=NetPosition(0),
        average_price=None,
        as_of=Timestamp.now(),
    )


def test_flat_position_rejects_average_price() -> None:
    with pytest.raises(InvalidPositionError):
        Position(
            account=ACCOUNT,
            instrument=Instrument.MXF,
            contract=CONTRACT,
            net=NetPosition(0),
            average_price=Price(Decimal("18500")),
            as_of=Timestamp.now(),
        )


def test_non_flat_position_requires_average_price() -> None:
    with pytest.raises(InvalidPositionError):
        Position(
            account=ACCOUNT,
            instrument=Instrument.MXF,
            contract=CONTRACT,
            net=NetPosition(1),
            average_price=None,
            as_of=Timestamp.now(),
        )


def test_order_carries_a_client_order_id_independent_of_broker_order_number() -> None:
    order = Order(
        client_order_id=ClientOrderId(uuid4()),
        account=ACCOUNT,
        instrument=Instrument.MXF,
        contract=CONTRACT,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("18500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
    )
    assert order.client_order_id.value is not None


def test_client_order_id_defaults_to_a_fresh_uuid() -> None:
    assert ClientOrderId().value != ClientOrderId().value
