from __future__ import annotations

import pytest

from tfx_quant.domain.errors import InvalidQuantityError
from tfx_quant.domain.quantity import MAX_LOTS, NetPosition, Quantity


@pytest.mark.parametrize("lots", [1, 2])
def test_quantity_accepts_legal_lots(lots: int) -> None:
    assert Quantity(lots).lots == lots


@pytest.mark.parametrize("lots", [0, -1, 3, MAX_LOTS + 1])
def test_quantity_rejects_illegal_lots(lots: int) -> None:
    with pytest.raises(InvalidQuantityError):
        Quantity(lots)


def test_quantity_rejects_non_int() -> None:
    with pytest.raises(InvalidQuantityError):
        Quantity(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("lots", [-2, -1, 0, 1, 2])
def test_net_position_accepts_legal_range(lots: int) -> None:
    assert NetPosition(lots).lots == lots


@pytest.mark.parametrize("lots", [-3, 3])
def test_net_position_rejects_beyond_cap(lots: int) -> None:
    with pytest.raises(InvalidQuantityError):
        NetPosition(lots)


def test_net_position_is_flat() -> None:
    assert NetPosition(0).is_flat
    assert not NetPosition(1).is_flat
