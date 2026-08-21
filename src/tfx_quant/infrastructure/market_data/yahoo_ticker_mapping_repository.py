"""JsonYahooTickerMappingRepository — the controlled 內部商品／契約 -> Yahoo ticker
backing store.

Loads `yahoo_ticker_mapping.example.json` (or an operator-supplied path — see
`TradingSettings.yahoo_ticker_mapping_path`) into the `YahooTickerMappingRepository`
port's shape. Mirrors `JsonTradingCalendarRepository`/`JsonInstrumentMasterRepository`'s
"version-controlled JSON, never guessed or computed" precedent — see
`application.ports.yahoo_ticker_mapping`'s module docstring for why no formula exists
here the way `domain.instrument_master.futures_quote_symbol()` exists for the Yuanta
quote symbol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.infrastructure.market_data.errors import YahooTickerMappingFileError


def _parse_entry(raw: dict[str, Any], *, index: int) -> tuple[Instrument, ContractMonth, str]:
    required = ("instrument", "contract_year", "contract_month", "yahoo_ticker")
    missing = [key for key in required if key not in raw]
    if missing:
        raise YahooTickerMappingFileError(f"第 {index} 筆 mapping 缺少欄位：{', '.join(missing)}")
    try:
        instrument = Instrument(raw["instrument"])
    except ValueError as exc:
        raise YahooTickerMappingFileError(
            f"第 {index} 筆 mapping 的 instrument 不合法：{raw['instrument']!r}"
        ) from exc
    try:
        contract = ContractMonth(year=int(raw["contract_year"]), month=int(raw["contract_month"]))
    except (DomainError, TypeError, ValueError) as exc:
        raise YahooTickerMappingFileError(
            f"第 {index} 筆 mapping 的 contract_year/contract_month 不合法："
            f"{raw['contract_year']!r}/{raw['contract_month']!r}"
        ) from exc
    ticker = raw["yahoo_ticker"]
    if not isinstance(ticker, str) or not ticker.strip():
        raise YahooTickerMappingFileError(f"第 {index} 筆 mapping 的 yahoo_ticker 不可為空")
    return instrument, contract, ticker


class JsonYahooTickerMappingRepository:
    """Implements `application.ports.yahoo_ticker_mapping.YahooTickerMappingRepository`."""

    def __init__(self, path: Path) -> None:
        raw_text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise YahooTickerMappingFileError(
                f"Yahoo ticker mapping {path} 不是合法 JSON：{exc}"
            ) from exc

        mappings_raw = raw.get("mappings")
        if not isinstance(mappings_raw, list):
            raise YahooTickerMappingFileError(f"Yahoo ticker mapping {path} 缺少 mappings 陣列")

        entries: dict[tuple[Instrument, ContractMonth], str] = {}
        for i, entry in enumerate(mappings_raw):
            instrument, contract, ticker = _parse_entry(entry, index=i)
            key = (instrument, contract)
            if key in entries:
                raise YahooTickerMappingFileError(
                    f"Yahoo ticker mapping {path} 有重複的 (instrument, contract)：{key}"
                )
            entries[key] = ticker
        self._entries = entries

    def get(self, instrument: Instrument, contract: ContractMonth) -> str | None:
        return self._entries.get((instrument, contract))
