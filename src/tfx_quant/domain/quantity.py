"""口數 — lot counts for orders and net positions.

The hard ceiling is 2 lots, system-wide, non-negotiable (see
`implementation prompt/README.md` global rules). `TradingSettings.max_net_lots` may
configure a stricter (lower) cap, but never above this.
"""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.errors import InvalidQuantityError

MAX_LOTS = 2


@dataclass(frozen=True, slots=True)
class Quantity:
    """A strictly positive order quantity, capped at MAX_LOTS."""

    lots: int

    def __post_init__(self) -> None:
        if not isinstance(self.lots, int) or isinstance(self.lots, bool):
            raise InvalidQuantityError(f"lots must be an int, got {type(self.lots).__name__}")
        if self.lots < 1:
            raise InvalidQuantityError(f"lots must be >= 1, got {self.lots}")
        if self.lots > MAX_LOTS:
            raise InvalidQuantityError(f"lots must be <= {MAX_LOTS}, got {self.lots}")


@dataclass(frozen=True, slots=True)
class NetPosition:
    """A signed net position in lots: positive = net long, negative = net short."""

    lots: int

    def __post_init__(self) -> None:
        if not isinstance(self.lots, int) or isinstance(self.lots, bool):
            raise InvalidQuantityError(f"lots must be an int, got {type(self.lots).__name__}")
        if abs(self.lots) > MAX_LOTS:
            raise InvalidQuantityError(f"|lots| must be <= {MAX_LOTS}, got {self.lots}")

    @property
    def is_flat(self) -> bool:
        return self.lots == 0
