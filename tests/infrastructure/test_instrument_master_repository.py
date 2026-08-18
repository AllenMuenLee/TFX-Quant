from __future__ import annotations

import json
from pathlib import Path

import pytest

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.infrastructure.yuanta.errors import InstrumentMasterFileError
from tfx_quant.infrastructure.yuanta.instrument_master_repository import (
    JsonInstrumentMasterRepository,
)

_EXAMPLE_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "tfx_quant"
    / "infrastructure"
    / "yuanta"
    / "instrument_master.example.json"
)

_VALID_ENTRY = {
    "instrument": "TXF",
    "contract": "202609",
    "vendor_symbol": "TXFU6",
    "broker_product_code": "TXF",
    "tick_size": "1",
    "multiplier": "200",
    "day_session_start": "08:45",
    "day_session_end": "13:45",
    "night_session_start": "15:00",
    "night_session_end": "05:00",
    "expiry_date": "2026-09-16",
    "tradable": True,
}


def _write(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "instrument_master.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_example_master_file_loads_successfully() -> None:
    repo = JsonInstrumentMasterRepository(_EXAMPLE_PATH)
    entries = repo.list_for(Instrument.TXF)
    assert len(entries) > 0
    assert all(entry.instrument is Instrument.TXF for entry in entries)


def test_get_returns_matching_entry(tmp_path: Path) -> None:
    path = _write(tmp_path, [_VALID_ENTRY])
    repo = JsonInstrumentMasterRepository(path)
    entry = repo.get(Instrument.TXF, ContractMonth(year=2026, month=9))
    assert entry is not None
    assert entry.vendor_symbol == "TXFU6"


def test_get_returns_none_for_unknown_pair(tmp_path: Path) -> None:
    path = _write(tmp_path, [_VALID_ENTRY])
    repo = JsonInstrumentMasterRepository(path)
    assert repo.get(Instrument.MXF, ContractMonth(year=2026, month=9)) is None


def test_list_for_empty_instrument_returns_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, [_VALID_ENTRY])
    repo = JsonInstrumentMasterRepository(path)
    assert repo.list_for(Instrument.MXF) == ()


def test_missing_entries_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_malformed_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    entry = dict(_VALID_ENTRY)
    del entry["vendor_symbol"]
    path = _write(tmp_path, [entry])
    with pytest.raises(InstrumentMasterFileError, match="vendor_symbol"):
        JsonInstrumentMasterRepository(path)


def test_invalid_instrument_code_raises(tmp_path: Path) -> None:
    entry = dict(_VALID_ENTRY)
    entry["instrument"] = "NOPE"
    path = _write(tmp_path, [entry])
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_invalid_contract_code_raises(tmp_path: Path) -> None:
    entry = dict(_VALID_ENTRY)
    entry["contract"] = "2026-09"
    path = _write(tmp_path, [entry])
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_invalid_decimal_raises(tmp_path: Path) -> None:
    entry = dict(_VALID_ENTRY)
    entry["tick_size"] = "not-a-number"
    path = _write(tmp_path, [entry])
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_invalid_expiry_date_raises(tmp_path: Path) -> None:
    entry = dict(_VALID_ENTRY)
    entry["expiry_date"] = "09/16/2026"
    path = _write(tmp_path, [entry])
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_domain_validation_failure_is_wrapped(tmp_path: Path) -> None:
    entry = dict(_VALID_ENTRY)
    entry["tick_size"] = "0"
    path = _write(tmp_path, [entry])
    with pytest.raises(InstrumentMasterFileError):
        JsonInstrumentMasterRepository(path)


def test_duplicate_entries_raise(tmp_path: Path) -> None:
    path = _write(tmp_path, [_VALID_ENTRY, dict(_VALID_ENTRY)])
    with pytest.raises(InstrumentMasterFileError, match="重複"):
        JsonInstrumentMasterRepository(path)
