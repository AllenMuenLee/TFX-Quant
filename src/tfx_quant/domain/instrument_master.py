"""商品主檔 — the controlled record of one tradable (instrument, contract month) pair.

Everything here is a pure value object: tick size, multiplier, session times, expiry
date, and tradability are all read from a controlled master file (see
`application.ports.instrument_master.InstrumentMasterRepository` and
`infrastructure.yuanta.instrument_master_repository`), never computed from a guessed
formula. `vendor_symbol` in particular — the broker's EasyWin-format quote/order code
(e.g. "TXFE9") — has no documented month/year encoding anywhere in the extracted
vendor PDFs; see `docs/adr/0005-instrument-master-and-selection.md` for why this
codebase treats it as controlled data rather than deriving it in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidInstrumentMasterEntryError
from tfx_quant.domain.instrument import Instrument


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
    """The broker's EasyWin-format quote/order symbol for this exact contract (e.g.
    "TXFE9") — passed verbatim to `AddMktReg`/`DelMktReg`/`FutNo`. Controlled data, not
    derived from a formula."""
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
