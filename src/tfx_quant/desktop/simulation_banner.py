"""A bright, always-visible bar stating that no real order can be sent.

Shown whenever `services.simulation` is true — i.e. the 測試環境, where the trade adapter
is the local simulator (no server) while market data is still the real Yuanta feed.
"""

from __future__ import annotations

import wx

from tfx_quant.desktop.composition import ServiceContainer

_BG = wx.Colour(217, 119, 6)


def banner_text(services: ServiceContainer) -> str | None:
    if not services.simulation:
        return None
    return "交易模擬／不會送出真單　·　行情：元大即時（真實）　·　委託與成交：本機模擬"


class SimulationBanner(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self.SetBackgroundColour(_BG)
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        label = wx.StaticText(self, label=banner_text(services) or "")
        label.SetForegroundColour(wx.WHITE)
        label.SetFont(label.GetFont().Bold())
        sizer.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8)
        self.SetSizer(sizer)


__all__ = ["SimulationBanner", "banner_text"]
