"""InstrumentSelectionPanel — Feature 03's instrument/contract selection UI.

Embedded in `ReadinessFrame`. Lets the operator pick 小台/大台 and either an explicit
contract month (MANUAL) or let the near-month be auto-resolved (AUTO) — either way,
Feature 03's acceptance criteria requires the resolved contract to be shown and
explicitly confirmed before it's actually subscribed to
(`InstrumentSelectionService.switch_to()`), even in AUTO mode. No order-sending
control lives here, matching this codebase's existing UI constraint — this is
instrument/session selection, not order submission.

All actions here are synchronous, operator-initiated button/choice handlers on the wx
UI thread — unlike `ReadinessFrame`'s broker-event subscriptions, nothing here needs
`wx.CallAfter`.
"""

from __future__ import annotations

import wx

from tfx_quant.application.instrument_selection.errors import InstrumentSelectionError
from tfx_quant.application.instrument_selection.selection import ResolvedSelection
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry

_MODE_AUTO = "近月自動 (AUTO)"
_MODE_MANUAL = "手動選擇 (MANUAL)"
_INSTRUMENTS: tuple[Instrument, ...] = tuple(Instrument)


class InstrumentSelectionPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services
        self._pending: ResolvedSelection | None = None
        self._contract_entries: list[InstrumentMasterEntry] = []

        sizer = wx.BoxSizer(wx.VERTICAL)

        current_row = wx.BoxSizer(wx.HORIZONTAL)
        current_row.Add(
            wx.StaticText(self, label="目前商品／契約："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._current_label = wx.StaticText(self, label="（尚未選擇）")
        current_row.Add(self._current_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer.Add(current_row, 0)

        pick_row = wx.BoxSizer(wx.HORIZONTAL)
        pick_row.Add(wx.StaticText(self, label="商品："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        self._instrument_choice = wx.Choice(
            self, choices=[i.display_name_zh for i in _INSTRUMENTS]
        )
        self._instrument_choice.SetSelection(0)
        self._instrument_choice.Bind(wx.EVT_CHOICE, self._on_instrument_or_mode_changed)
        pick_row.Add(self._instrument_choice, 0, wx.ALL, 6)

        self._mode_choice = wx.Choice(self, choices=[_MODE_AUTO, _MODE_MANUAL])
        self._mode_choice.SetSelection(0)
        self._mode_choice.Bind(wx.EVT_CHOICE, self._on_instrument_or_mode_changed)
        pick_row.Add(self._mode_choice, 0, wx.ALL, 6)

        self._contract_choice = wx.Choice(self, choices=[])
        self._contract_choice.Bind(wx.EVT_CHOICE, self._on_contract_choice_changed)
        pick_row.Add(self._contract_choice, 0, wx.ALL, 6)
        sizer.Add(pick_row, 0)

        self._resolved_label = wx.StaticText(self, label="")
        sizer.Add(self._resolved_label, 0, wx.ALL, 6)

        self._confirm_button = wx.Button(self, label="確認並切換 (Confirm && Switch)")
        self._confirm_button.Bind(wx.EVT_BUTTON, self._on_confirm)
        sizer.Add(self._confirm_button, 0, wx.ALL, 6)

        self._status_label = wx.StaticText(self, label="")
        sizer.Add(self._status_label, 0, wx.ALL, 6)

        self.SetSizer(sizer)
        self._refresh_contract_choices()
        self._resolve_pending()
        self._refresh()

    # -- Selection --------------------------------------------------------------

    def _selected_instrument(self) -> Instrument:
        return _INSTRUMENTS[self._instrument_choice.GetSelection()]

    def _is_auto_mode(self) -> bool:
        return self._mode_choice.GetSelection() == 0

    def _on_instrument_or_mode_changed(self, _event: wx.CommandEvent) -> None:
        self._refresh_contract_choices()
        self._resolve_pending()

    def _refresh_contract_choices(self) -> None:
        auto = self._is_auto_mode()
        self._contract_choice.Enable(not auto)
        if auto:
            self._contract_entries = []
            self._contract_choice.Set([])
            return
        instrument = self._selected_instrument()
        entries = [
            entry
            for entry in self._services.instrument_master.list_for(instrument)
            if entry.tradable
        ]
        entries.sort(key=lambda entry: (entry.contract.year, entry.contract.month))
        self._contract_entries = entries
        self._contract_choice.Set([entry.display_name_zh for entry in entries])
        if entries:
            self._contract_choice.SetSelection(0)

    def _on_contract_choice_changed(self, _event: wx.CommandEvent) -> None:
        self._resolve_pending()

    def _resolve_pending(self) -> None:
        service = self._services.instrument_selection
        instrument = self._selected_instrument()
        try:
            if self._is_auto_mode():
                self._pending = service.resolve_near_month(instrument)
            else:
                index = self._contract_choice.GetSelection()
                if index == wx.NOT_FOUND or not self._contract_entries:
                    self._pending = None
                    self._resolved_label.SetLabel("（請選擇契約月份）")
                    return
                contract = self._contract_entries[index].contract
                self._pending = service.resolve_manual(instrument, contract)
        except InstrumentSelectionError as exc:
            self._pending = None
            self._resolved_label.SetLabel(f"無法解析契約：{exc}")
            return

        entry = self._pending.entry
        self._resolved_label.SetLabel(
            f"解析結果：{entry.display_name_zh}｜券商代碼 {entry.broker_product_code}"
            f"｜行情代碼 {entry.vendor_symbol}（請確認後再切換）"
        )

    # -- Switching --------------------------------------------------------------

    def _on_confirm(self, _event: wx.CommandEvent) -> None:
        if self._pending is None:
            self._resolve_pending()
        if self._pending is None:
            return
        service = self._services.instrument_selection
        try:
            service.switch_to(self._pending)
        except InstrumentSelectionError as exc:
            self._status_label.SetLabel(f"切換失敗：{exc}")
            self._refresh()
            return
        self._status_label.SetLabel("切換成功，請重新啟動策略。")
        self._refresh()

    # -- Refresh ------------------------------------------------------------------

    def refresh(self) -> None:
        """Called by `ReadinessFrame` whenever a broker session event arrives —
        `check_switch_allowed()`'s answer can change as login/query state changes."""
        self._refresh()

    def _refresh(self) -> None:
        service = self._services.instrument_selection
        current = service.current
        if current is None:
            self._current_label.SetLabel("（尚未選擇）")
        else:
            entry = current.entry
            self._current_label.SetLabel(
                f"{entry.display_name_zh}｜券商代碼 {entry.broker_product_code}"
                f"｜行情代碼 {entry.vendor_symbol}"
            )
        reason = service.check_switch_allowed()
        self._confirm_button.Enable(reason is None)
        if reason is not None:
            self._status_label.SetLabel(f"目前無法切換：{reason}")
