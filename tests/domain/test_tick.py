from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidTickError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.tick import Tick
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_AT = Timestamp(datetime(2026, 9, 16, 9, 0, tzinfo=TAIPEI_TZ))


def _tick(**overrides: object) -> Tick:
    defaults: dict[str, object] = {
        "instrument": Instrument.TXF,
        "contract": ContractMonth(year=2026, month=9),
        "at": _AT,
        "price": Price(Decimal("17500")),
        "size": 1,
        "serial_no": 100,
    }
    defaults.update(overrides)
    return Tick(**defaults)  # type: ignore[arg-type]


def test_valid_tick_constructs() -> None:
    tick = _tick()
    assert tick.size == 1
    assert tick.serial_no == 100


def test_rejects_negative_size() -> None:
    with pytest.raises(InvalidTickError):
        _tick(size=-1)


def test_rejects_non_positive_serial_no() -> None:
    with pytest.raises(InvalidTickError):
        _tick(serial_no=0)


def test_allows_zero_size_snapshot_push() -> None:
    _tick(size=0)
