"""商品 — the futures products this system is allowed to trade."""

from __future__ import annotations

from enum import StrEnum


class Instrument(StrEnum):
    """Exactly one of these may be the currently selected instrument at a time."""

    MXF = "MXF"
    """小台指 — Mini Taiwan Index Futures."""

    TXF = "TXF"
    """大台指 — Taiwan Index Futures."""

    @property
    def display_name_zh(self) -> str:
        """The Chinese product name shown on screen (see Feature 03's acceptance
        criteria: 畫面需同時顯示中文商品名、券商商品代碼及契約月份)."""
        return _DISPLAY_NAME_ZH[self]


_DISPLAY_NAME_ZH: dict[Instrument, str] = {
    Instrument.MXF: "小台指",
    Instrument.TXF: "大台指",
}
