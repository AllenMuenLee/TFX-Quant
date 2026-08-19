from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidInstrumentMasterEntryError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry, futures_quote_symbol


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


def test_default_order_commodity_code_is_unresolved() -> None:
    assert _entry().order_commodity_code == ""


def test_futures_quote_symbol_matches_docs_worked_example() -> None:
    # 期貨報價代碼7xxx變更規則 docs page's own worked example: "2021年台指期6月:TXFF1".
    assert futures_quote_symbol(Instrument.TXF, ContractMonth(year=2021, month=6)) == "TXFF1"


@pytest.mark.parametrize(
    ("month", "year", "expected_code"),
    [
        (1, 2026, "A6"),
        (8, 2026, "H6"),
        (9, 2026, "I6"),
        (12, 2026, "L6"),
        (3, 2027, "C7"),
    ],
)
def test_futures_quote_symbol_month_year_codes(month: int, year: int, expected_code: str) -> None:
    assert (
        futures_quote_symbol(Instrument.MXF, ContractMonth(year=year, month=month))
        == f"MXF{expected_code}"
    )
