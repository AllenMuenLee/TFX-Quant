from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidInstrumentMasterEntryError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry


def _entry(**overrides: object) -> InstrumentMasterEntry:
    defaults: dict[str, object] = {
        "instrument": Instrument.TXF,
        "contract": ContractMonth(year=2026, month=9),
        "vendor_symbol": "TXFU6",
        "broker_product_code": "TXF",
        "tick_size": Decimal("1"),
        "multiplier": Decimal("200"),
        "day_session_start": time(8, 45),
        "day_session_end": time(13, 45),
        "night_session_start": time(15, 0),
        "night_session_end": time(5, 0),
        "expiry_date": date(2026, 9, 16),
        "tradable": True,
    }
    defaults.update(overrides)
    return InstrumentMasterEntry(**defaults)  # type: ignore[arg-type]


def test_instrument_display_name_zh() -> None:
    assert Instrument.MXF.display_name_zh == "小台指"
    assert Instrument.TXF.display_name_zh == "大台指"


def test_valid_entry_constructs() -> None:
    entry = _entry()
    assert entry.display_name_zh == "大台指 2026年09月"


def test_rejects_blank_vendor_symbol() -> None:
    with pytest.raises(InvalidInstrumentMasterEntryError):
        _entry(vendor_symbol="  ")


def test_rejects_blank_broker_product_code() -> None:
    with pytest.raises(InvalidInstrumentMasterEntryError):
        _entry(broker_product_code="")


@pytest.mark.parametrize("field", ["tick_size", "multiplier"])
def test_rejects_non_positive_decimal_fields(field: str) -> None:
    with pytest.raises(InvalidInstrumentMasterEntryError):
        _entry(**{field: Decimal("0")})


def test_rejects_non_decimal_tick_size() -> None:
    with pytest.raises(InvalidInstrumentMasterEntryError):
        _entry(tick_size=1)


def test_rejects_day_session_start_after_end() -> None:
    with pytest.raises(InvalidInstrumentMasterEntryError):
        _entry(day_session_start=time(14, 0), day_session_end=time(13, 45))


def test_rejects_partial_night_session() -> None:
    with pytest.raises(InvalidInstrumentMasterEntryError):
        _entry(night_session_start=time(15, 0), night_session_end=None)


def test_allows_no_night_session() -> None:
    entry = _entry(night_session_start=None, night_session_end=None)
    assert entry.night_session_start is None
