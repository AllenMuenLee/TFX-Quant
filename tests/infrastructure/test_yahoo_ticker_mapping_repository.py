from __future__ import annotations

import json
from pathlib import Path

import pytest

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.infrastructure.market_data.errors import YahooTickerMappingFileError
from tfx_quant.infrastructure.market_data.yahoo_ticker_mapping_repository import (
    JsonYahooTickerMappingRepository,
)

_EXAMPLE_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "tfx_quant"
    / "infrastructure"
    / "market_data"
    / "yahoo_ticker_mapping.example.json"
)


def _write(tmp_path: Path, content: dict[str, object]) -> Path:
    path = tmp_path / "yahoo_ticker_mapping.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_example_mapping_file_loads_successfully_and_is_deliberately_empty() -> None:
    """No confirmed Yahoo ticker exists for any TAIFEX futures contract as of writing
    (see the example file's own warning) — the bundled example must load without error
    but resolve nothing, so backfill degrades to "leave every gap" until an operator
    supplies a real, confirmed mapping."""
    repo = JsonYahooTickerMappingRepository(_EXAMPLE_PATH)
    assert repo.get(Instrument.TXF, ContractMonth(year=2026, month=9)) is None
    assert repo.get(Instrument.MXF, ContractMonth(year=2026, month=9)) is None


def test_mapping_entry_is_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "mappings": [
                {
                    "instrument": "TXF",
                    "contract_year": 2026,
                    "contract_month": 9,
                    "yahoo_ticker": "TXF=F",
                }
            ]
        },
    )
    repo = JsonYahooTickerMappingRepository(path)
    assert repo.get(Instrument.TXF, ContractMonth(year=2026, month=9)) == "TXF=F"


def test_unmapped_pair_returns_none(tmp_path: Path) -> None:
    path = _write(tmp_path, {"mappings": []})
    repo = JsonYahooTickerMappingRepository(path)
    assert repo.get(Instrument.MXF, ContractMonth(year=2026, month=9)) is None


def test_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)


def test_rejects_missing_mappings_array(tmp_path: Path) -> None:
    path = _write(tmp_path, {})
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)


def test_rejects_missing_required_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path, {"mappings": [{"instrument": "TXF", "contract_year": 2026, "contract_month": 9}]}
    )
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)


def test_rejects_invalid_instrument(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "mappings": [
                {
                    "instrument": "NOT_A_PRODUCT",
                    "contract_year": 2026,
                    "contract_month": 9,
                    "yahoo_ticker": "TXF=F",
                }
            ]
        },
    )
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)


def test_rejects_invalid_contract_month(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "mappings": [
                {
                    "instrument": "TXF",
                    "contract_year": 2026,
                    "contract_month": 13,
                    "yahoo_ticker": "TXF=F",
                }
            ]
        },
    )
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)


def test_rejects_blank_ticker(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "mappings": [
                {
                    "instrument": "TXF",
                    "contract_year": 2026,
                    "contract_month": 9,
                    "yahoo_ticker": "",
                }
            ]
        },
    )
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)


def test_rejects_duplicate_instrument_contract_pair(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "mappings": [
                {
                    "instrument": "TXF",
                    "contract_year": 2026,
                    "contract_month": 9,
                    "yahoo_ticker": "TXF=F",
                },
                {
                    "instrument": "TXF",
                    "contract_year": 2026,
                    "contract_month": 9,
                    "yahoo_ticker": "TXF2=F",
                },
            ]
        },
    )
    with pytest.raises(YahooTickerMappingFileError):
        JsonYahooTickerMappingRepository(path)
