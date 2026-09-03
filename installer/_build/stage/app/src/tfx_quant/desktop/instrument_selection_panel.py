"""Market-quote instrument selector with automatic near-month resolution."""

from __future__ import annotations

import wx

from tfx_quant.application.instrument_selection.errors import InstrumentSelectionError
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument

_INSTRUMENTS = tuple(Instrument)


def _display_contract(contract: ContractMonth) -> str:
    return f"{contract.year:04d}-{contract.month:02d}"


class InstrumentSelectionPanel(wx.Panel):
    """Switches only the displayed/subscribed quote product.

    Contract selection is always the controlled master's nearest tradable month. The
    operator can choose the quote product, but never a contract month. Trading remains
    independently constrained to MXF at the order boundary.
    """

    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services

        outer = wx.BoxSizer(wx.HORIZONTAL)
        label = wx.StaticText(self, label="市場行情")
        label.SetFont(label.GetFont().Bold())
        outer.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self._instrument_choice = wx.Choice(
            self, choices=[item.display_name_zh for item in _INSTRUMENTS]
        )
        self._instrument_choice.SetSelection(0)
        outer.Add(self._instrument_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self._switch_button = wx.Button(self, label="切換行情")
        self._switch_button.Bind(wx.EVT_BUTTON, self._on_switch)
        outer.Add(self._switch_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self._status = wx.StaticText(self, label="契約年月：自動選擇中")
        outer.Add(self._status, 0, wx.ALIGN_CENTER_VERTICAL)

        self.SetSizer(outer)
        self.refresh()

    def _selected_instrument(self) -> Instrument:
        return _INSTRUMENTS[self._instrument_choice.GetSelection()]

    def _on_switch(self, _event: wx.CommandEvent) -> None:
        instrument = self._selected_instrument()
        try:
            resolved = self._services.instrument_selection.resolve_near_month(instrument)
            self._services.instrument_selection.switch_to(resolved)
        except InstrumentSelectionError as exc:
            self._status.SetLabel(str(exc))
            return
        self._status.SetLabel(f"契約年月：{_display_contract(resolved.contract)}（自動近月）")

    def refresh(self) -> None:
        current = self._services.instrument_selection.current
        if current is None:
            self._status.SetLabel("契約年月：尚未解析")
            return
        self._instrument_choice.SetSelection(_INSTRUMENTS.index(current.instrument))
        self._status.SetLabel(f"契約年月：{_display_contract(current.contract)}（自動近月）")
