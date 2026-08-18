from __future__ import annotations

from decimal import Decimal

import pytest

from tfx_quant.domain.errors import InvalidMoneyError
from tfx_quant.domain.money import Money, Price


def test_price_accepts_positive_decimal() -> None:
    assert Price(Decimal("18500.0")).amount == Decimal("18500.0")


def test_price_rejects_float() -> None:
    with pytest.raises(InvalidMoneyError):
        Price(18500.0)  # type: ignore[arg-type]


def test_price_rejects_zero_or_negative() -> None:
    with pytest.raises(InvalidMoneyError):
        Price(Decimal("0"))
    with pytest.raises(InvalidMoneyError):
        Price(Decimal("-1"))


def test_money_rejects_float() -> None:
    with pytest.raises(InvalidMoneyError):
        Money(100.0)  # type: ignore[arg-type]


def test_money_allows_negative_for_realized_loss() -> None:
    assert Money(Decimal("-500")).amount == Decimal("-500")


def test_money_addition_and_subtraction_stay_decimal() -> None:
    total = Money(Decimal("100")) + Money(Decimal("50")) - Money(Decimal("30"))
    assert total.amount == Decimal("120")
