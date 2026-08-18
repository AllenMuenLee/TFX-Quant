"""MarketDataPanel — Feature 04's forming/closed-bar and staleness display.

Embedded in `ReadinessFrame`, mirroring `InstrumentSelectionPanel`'s embedding: purely a
display surface (no order-sending control, matching this codebase's existing UI
constraint) with a `refresh()` the parent frame calls. `ReadinessFrame` — not this panel
— owns the market-data event subscriptions (`BarClosed`/`MarketDataTickReceived`/
`MarketDataFreshnessChanged`/`MarketDataGapDetected`/`MarketDataGapCleared`) and their
`wx.CallAfter` hop off the `EventCoordinator` consumer thread, same as it already does
for broker session events — keeping subscription lifecycle in one place (the frame's
existing `_on_close` teardown) rather than duplicating it per embedded panel.
"""

from __future__ import annotations

import wx

from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.bar import Bar, CandleColor

_CANDLE_LABEL_ZH = {
    CandleColor.RED: "紅",
    CandleColor.BLACK: "黑",
    CandleColor.DOJI: "十字",
}
_RECENT_BARS_SHOWN = 10


def _format_bar(bar: Bar) -> str:
    label = bar.start.value.strftime("%m/%d %H:%M")
    color = _CANDLE_LABEL_ZH[bar.candle_color]
    return (
        f"{label}｜開 {bar.open.amount} 高 {bar.high.amount} 低 {bar.low.amount} "
        f"收 {bar.close.amount}｜量 {bar.volume}｜{color}"
    )


class MarketDataPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services

        sizer = wx.BoxSizer(wx.VERTICAL)

        status_row = wx.BoxSizer(wx.HORIZONTAL)
        status_row.Add(
            wx.StaticText(self, label="行情狀態："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._status_label = wx.StaticText(self, label="（尚未選擇契約）")
        status_row.Add(self._status_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer.Add(status_row, 0)

        forming_row = wx.BoxSizer(wx.HORIZONTAL)
        forming_row.Add(
            wx.StaticText(self, label="目前 K 棒："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._forming_label = wx.StaticText(self, label="（尚無資料）")
        forming_row.Add(self._forming_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer.Add(forming_row, 0)

        sizer.Add(wx.StaticText(self, label="最近收盤 K 棒："), 0, wx.ALL, 6)
        self._recent_list = wx.ListBox(self, size=(-1, 140))
        sizer.Add(self._recent_list, 0, wx.EXPAND | wx.ALL, 6)

        self.SetSizer(sizer)
        self._refresh()

    def refresh(self) -> None:
        """Called by `ReadinessFrame` on every broker/instrument-selection/market-data
        event — see module docstring for why the subscriptions live there."""
        self._refresh()

    def _refresh(self) -> None:
        current = self._services.instrument_selection.current
        if current is None:
            self._status_label.SetLabel("（尚未選擇契約）")
            self._forming_label.SetLabel("（尚無資料）")
            self._recent_list.Set([])
            return

        service = self._services.market_data_bar_service
        instrument, contract = current.instrument, current.contract

        is_stale = service.is_stale(instrument, contract)
        has_gap = service.has_gap(instrument, contract)
        last_update = service.last_update_at(instrument, contract)
        last_update_str = "無" if last_update is None else last_update.value.strftime("%H:%M:%S")
        status_parts = [
            "STALE" if is_stale else "FRESH",
            f"最後更新 {last_update_str}",
        ]
        if has_gap:
            status_parts.append("GAP — 資料缺口，暫無法確認 K 棒完整性")
        self._status_label.SetLabel("｜".join(status_parts))

        forming = service.forming_bar(instrument, contract)
        self._forming_label.SetLabel("（尚無資料）" if forming is None else _format_bar(forming))

        recent = service.recent_closed_bars(instrument, contract, limit=_RECENT_BARS_SHOWN)
        self._recent_list.Set([_format_bar(bar) for bar in reversed(recent)])
