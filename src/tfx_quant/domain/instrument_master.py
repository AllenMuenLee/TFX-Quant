"""商品主檔 — the controlled record of one tradable (instrument, contract month) pair.

Tick size, multiplier, session times, expiry date, and tradability all still come from
a controlled master file (see `application.ports.instrument_master.
InstrumentMasterRepository` and `infrastructure.yuanta.instrument_master_repository`)
— none of those are safely derivable, and never will be (see
`docs/adr/0005-instrument-master-and-selection.md`).

`vendor_symbol` (the SPARK API real-time quote symbol, e.g. "TXFH6") is different: the
official docs (期貨報價代碼7xxx變更規則 page) now document its encoding precisely — see
`futures_quote_symbol()` below. This is a genuine SPARK-API-pivot change from the old
legacy OCX API, whose EasyWin-format vendor symbol had no documented month/year
encoding anywhere in the vendor PDFs (ADR 0005's original reasoning for treating it as
pure controlled data). The master file still *stores* `vendor_symbol` as a literal
field rather than computing it inline in `InstrumentMasterEntry` — this keeps the
dataclass a plain value object and lets an operator eyeball/override a symbol directly
in the JSON — but every value in `instrument_master.example.json` is now
`futures_quote_symbol()`'s actual output, not a guess.

`order_commodity_code` (SPARK API's `SendFutureOrder.CommodityID1`, e.g. "FITX" for
TXF) is a **separate code space from `vendor_symbol`** — confirmed by the 交易 > 國內期貨
下單 docs page's own worked example (`CommodityID1 = 'FITX'` alongside a completely
different-looking quote symbol format). Only TXF's code is confirmed from that worked
example; other instruments' codes need operator confirmation against the vendor's
`FunctionList.xls` (股名檔對照表) before being marked confirmed — same posture as the
old `-UNCONFIRMED` convention this file used for `vendor_symbol`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidInstrumentMasterEntryError
from tfx_quant.domain.instrument import Instrument

_FUTURES_MONTH_CODE = "1ABCDEFGHIJKL"
"""期貨月份對應代碼 (期貨報價代碼7xxx變更規則): index 0 ("1") means 近月 (front/nearest
month) — a rolling alias this codebase deliberately never uses, since the instrument
master models one row per explicit contract month and a rolling alias's meaning shifts
across rollovers. Index `month` (1-12) gives that calendar month's letter, e.g.
index 6 ("F") = 六月/June, matching the docs' own worked example below."""


def futures_quote_symbol(instrument: Instrument, contract: ContractMonth) -> str:
    """The SPARK API real-time quote symbol for a specific futures contract month:
    `<root><month-code><year-digit>` per the 期貨報價代碼7xxx變更規則 docs page. E.g.
    `futures_quote_symbol(Instrument.TXF, ContractMonth(2021, 6))` == "TXFF1", matching
    the docs' own worked example ("2021年台指期6月:TXFF1") exactly.

    Deliberately never uses the docs' "近月" (front-month) alias code ("1") — see
    `_FUTURES_MONTH_CODE`'s docstring. `year-digit` is the last digit of the calendar
    year, per the docs ("為西元年數末1碼，如1(為2021年), 2(為2022年)").
    """
    month_code = _FUTURES_MONTH_CODE[contract.month]
    year_digit = contract.year % 10
    return f"{instrument.value}{month_code}{year_digit}"


def _require_positive_decimal(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise InvalidInstrumentMasterEntryError(
            f"{label} must be a decimal.Decimal, got {type(value).__name__}"
        )
    if value <= 0:
        raise InvalidInstrumentMasterEntryError(f"{label} must be > 0, got {value}")


@dataclass(frozen=True, slots=True)
class InstrumentMasterEntry:
    """One 商品主檔 row: everything needed to trade a specific contract month safely."""

    instrument: Instrument
    contract: ContractMonth
    vendor_symbol: str
    """The SPARK API real-time quote symbol for this exact contract (e.g. "TXFH6"),
    passed verbatim to `SubscribeStockTick`/`UnSubscribeStockTick`. Stored as a literal
    field rather than always recomputed inline, but every value in
    `instrument_master.example.json` is `futures_quote_symbol()`'s actual output — see
    this module's docstring."""
    broker_product_code: str
    """The broker's base product code shown on screen (e.g. "TXF"/"MXF"), distinct
    from `vendor_symbol` which also encodes the contract month."""
    tick_size: Decimal
    multiplier: Decimal
    """TWD per index point per contract (e.g. 200 for TXF, 50 for MXF)."""
    day_session_start: time
    day_session_end: time
    night_session_start: time | None
    night_session_end: time | None
    expiry_date: date
    """The last tradable date for this contract, straight from the controlled master
    file (not re-derived from a "3rd Wednesday" rule in code — TAIFEX occasionally
    shifts settlement dates around holidays)."""
    tradable: bool
    """Operator-controlled kill switch (到期、停止交易 etc.) — independent of
    `expiry_date` so a halt can be flagged before/without a code or data-file change."""
    order_commodity_code: str = ""
    """SPARK API's `SendFutureOrder.CommodityID1` (e.g. "FITX" for TXF) — a separate
    code space from `vendor_symbol`, needed for order placement (Feature 06), not
    quote subscription. Defaults to "" (unresolved) since not every instrument's code
    is confirmed yet — see this module's docstring. Feature 06 must refuse to place an
    order for an entry with an empty `order_commodity_code`."""

    def __post_init__(self) -> None:
        if not self.vendor_symbol.strip():
            raise InvalidInstrumentMasterEntryError("vendor_symbol must not be empty")
        if not self.broker_product_code.strip():
            raise InvalidInstrumentMasterEntryError("broker_product_code must not be empty")
        _require_positive_decimal(self.tick_size, "tick_size")
        _require_positive_decimal(self.multiplier, "multiplier")
        if self.day_session_start >= self.day_session_end:
            raise InvalidInstrumentMasterEntryError(
                "day_session_start must be before day_session_end"
            )
        if (self.night_session_start is None) != (self.night_session_end is None):
            raise InvalidInstrumentMasterEntryError(
                "night_session_start and night_session_end must both be set or both be None"
            )

    @property
    def display_name_zh(self) -> str:
        """E.g. "大台指 2026年09月" — see Feature 03's acceptance criteria."""
        name = self.instrument.display_name_zh
        return f"{name} {self.contract.year}年{self.contract.month:02d}月"
