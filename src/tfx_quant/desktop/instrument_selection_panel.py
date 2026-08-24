"""Compact automatic near-month instrument selector."""

from __future__ import annotations

import wx

from tfx_quant.application.instrument_selection.errors import InstrumentSelectionError
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.instrument import Instrument

_INSTRUMENTS = tuple(Instrument)


class InstrumentSelectionPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services
        row = wx.BoxSizer(wx.HORIZONTAL)
        label = wx.StaticText(self, label="監看商品")
        label.SetFont(label.GetFont().Bold())
        row.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._choice = wx.Choice(self, choices=[item.display_name_zh for item in _INSTRUMENTS])
        self._choice.SetSelection(0)
        current = services.instrument_selection.current
        if current is not None:
            self._choice.SetSelection(_INSTRUMENTS.index(current.instrument))
        row.Add(self._choice, 0, wx.RIGHT, 8)
        button = wx.Button(self, label="切換")
        button.Bind(wx.EVT_BUTTON, self._switch)
        row.Add(button, 0, wx.RIGHT, 12)
        self._status = wx.StaticText(self, label="契約月份由系統自動選擇")
        row.Add(self._status, 0, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(row)
        self.refresh()

    def _switch(self, _event: wx.CommandEvent) -> None:
        instrument = _INSTRUMENTS[self._choice.GetSelection()]
        try:
            resolved = self._services.instrument_selection.resolve_near_month(instrument)
            self._services.instrument_selection.switch_to(resolved)
            self._status.SetLabel(f"已切換至自動近月 {resolved.contract.code}")
        except InstrumentSelectionError as exc:
            self._status.SetLabel(str(exc))

    def refresh(self) -> None:
        current = self._services.instrument_selection.current
        if current is not None:
            self._choice.SetSelection(_INSTRUMENTS.index(current.instrument))
            self._status.SetLabel(f"自動近月 {current.contract.code}")
