"""JsonInstrumentMasterRepository — the controlled 商品主檔 backing store.

Loads a JSON file (see `instrument_master.example.json`) into `InstrumentMasterEntry`
value objects. This is a plain file reader — no vendor SDK dependency — but it lives
under `infrastructure.yuanta` rather than the generic `infrastructure` package because
its content is inherently Yuanta-specific data.

**Why a controlled file instead of a live vendor query**: the SPARK API docs
(`http://www.yuanta.com.tw/file-repository/content/sparkapi_docs/`) document no "list
tradable contracts"/"commodity master" call anywhere in 行情/交易/帳務 — every query
found is a quote/order/position/report operation, not a product catalog. The
implementation prompt explicitly allows sourcing this from either the vendor API *or* a
controlled master file ("元大 API 或受控商品主檔") — with no live API to call, this
repository is the only honest option. `vendor_symbol` itself, however, is no longer an
undecipherable vendor code needing manual confirmation — see
`docs/adr/0005-instrument-master-and-selection.md`'s addendum and
`domain.instrument_master.futures_quote_symbol()`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.infrastructure.yuanta.errors import InstrumentMasterFileError
from tfx_quant.telemetry import get_logger, log_error, log_info

_logger = get_logger(__name__)

_REQUIRED_FIELDS = (
    "instrument",
    "contract",
    "vendor_symbol",
    "broker_product_code",
    "tick_size",
    "multiplier",
    "day_session_start",
    "day_session_end",
    "night_session_start",
    "night_session_end",
    "expiry_date",
    "tradable",
)


def _parse_time(value: str | None, *, label: str) -> time | None:
    if value is None:
        return None
    try:
        hour_str, minute_str = value.split(":", 1)
        return time(int(hour_str), int(minute_str))
    except ValueError as exc:
        raise InstrumentMasterFileError(f"{label} 格式錯誤（需為 HH:MM），得到 {value!r}") from exc


def _parse_entry(raw: dict[str, Any], *, index: int) -> InstrumentMasterEntry:
    missing = [field for field in _REQUIRED_FIELDS if field not in raw]
    if missing:
        raise InstrumentMasterFileError(f"第 {index} 筆商品主檔缺少欄位：{', '.join(missing)}")

    try:
        instrument = Instrument(raw["instrument"])
    except ValueError as exc:
        raise InstrumentMasterFileError(
            f"第 {index} 筆商品主檔 instrument 不是有效商品代碼：{raw['instrument']!r}"
        ) from exc

    try:
        contract = ContractMonth.from_code(str(raw["contract"]))
    except DomainError as exc:
        raise InstrumentMasterFileError(f"第 {index} 筆商品主檔 contract 格式錯誤：{exc}") from exc

    try:
        tick_size = Decimal(str(raw["tick_size"]))
        multiplier = Decimal(str(raw["multiplier"]))
    except InvalidOperation as exc:
        raise InstrumentMasterFileError(
            f"第 {index} 筆商品主檔 tick_size/multiplier 不是有效數字"
        ) from exc

    try:
        expiry_date = date.fromisoformat(str(raw["expiry_date"]))
    except ValueError as exc:
        raise InstrumentMasterFileError(
            f"第 {index} 筆商品主檔 expiry_date 格式錯誤（需為 YYYY-MM-DD）：{raw['expiry_date']!r}"
        ) from exc

    try:
        return InstrumentMasterEntry(
            instrument=instrument,
            contract=contract,
            vendor_symbol=str(raw["vendor_symbol"]),
            broker_product_code=str(raw["broker_product_code"]),
            tick_size=tick_size,
            multiplier=multiplier,
            day_session_start=_parse_time(raw["day_session_start"], label="day_session_start")
            or time(0, 0),
            day_session_end=_parse_time(raw["day_session_end"], label="day_session_end")
            or time(0, 0),
            night_session_start=_parse_time(
                raw["night_session_start"], label="night_session_start"
            ),
            night_session_end=_parse_time(raw["night_session_end"], label="night_session_end"),
            expiry_date=expiry_date,
            tradable=bool(raw["tradable"]),
            order_commodity_code=str(raw.get("order_commodity_code", "")),
        )
    except DomainError as exc:
        raise InstrumentMasterFileError(f"第 {index} 筆商品主檔內容不合法：{exc}") from exc


class JsonInstrumentMasterRepository:
    """Implements `application.ports.instrument_master.InstrumentMasterRepository`."""

    def __init__(self, path: Path) -> None:
        try:
            self._load(path)
        except InstrumentMasterFileError as exc:
            log_error(
                _logger,
                "instrument_master_load_failed",
                source=str(path),
                error=str(exc),
            )
            raise

    def _load(self, path: Path) -> None:
        raw_text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise InstrumentMasterFileError(f"商品主檔 {path} 不是合法 JSON：{exc}") from exc

        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list):
            raise InstrumentMasterFileError(f"商品主檔 {path} 缺少 entries 陣列")

        entries = [_parse_entry(entry, index=i) for i, entry in enumerate(entries_raw)]

        by_key: dict[tuple[Instrument, ContractMonth], InstrumentMasterEntry] = {}
        for entry in entries:
            key = (entry.instrument, entry.contract)
            if key in by_key:
                raise InstrumentMasterFileError(
                    f"商品主檔 {path} 有重複項目：{entry.instrument.value} {entry.contract.code}"
                )
            by_key[key] = entry

        self._by_key = by_key
        self._by_instrument: dict[Instrument, list[InstrumentMasterEntry]] = {}
        for entry in entries:
            self._by_instrument.setdefault(entry.instrument, []).append(entry)

        # No explicit version field in the file format (see the file's own `_comment`
        # seed data) — the source file's own mtime is the only honest "which revision
        # of the master data is this" proxy available, rather than inventing a schema
        # field that doesn't exist.
        log_info(
            _logger,
            "instrument_master_loaded",
            source=str(path),
            master_data_version=str(path.stat().st_mtime),
            entry_count=len(entries),
            instruments=sorted({entry.instrument.value for entry in entries}),
        )

    def get(self, instrument: Instrument, contract: ContractMonth) -> InstrumentMasterEntry | None:
        return self._by_key.get((instrument, contract))

    def list_for(self, instrument: Instrument) -> Sequence[InstrumentMasterEntry]:
        return tuple(self._by_instrument.get(instrument, ()))
