"""價格與金額 — Decimal-backed value objects. Floats are never a valid representation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tfx_quant.domain.errors import InvalidMoneyError


def _require_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise InvalidMoneyError(
            f"{label} must be a decimal.Decimal (never float/int/str), got {type(value).__name__}"
        )
    return value


@dataclass(frozen=True, slots=True)
class Price:
    """A strictly positive price. Decimal-backed — no float path exists."""

    amount: Decimal

    def __post_init__(self) -> None:
        amount = _require_decimal(self.amount, "amount")
        if amount <= 0:
            raise InvalidMoneyError(f"Price amount must be > 0, got {amount}")


@dataclass(frozen=True, slots=True)
class Money:
    """A TWD-denominated amount (may be negative, e.g. realized loss). Decimal-backed."""

    amount: Decimal

    def __post_init__(self) -> None:
        _require_decimal(self.amount, "amount")

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - other.amount)
