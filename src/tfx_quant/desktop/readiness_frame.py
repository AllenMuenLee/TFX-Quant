"""Main desktop dashboard: market data first, broker login optional."""

from __future__ import annotations

import wx

from tfx_quant.application.events.events import (
    BarBackfillCompleted,
    BarClosed,
    BrokerLoggedOut,
    BrokerLoginSucceeded,
    BrokerSessionReady,
    InstrumentSwitchCompleted,
    MarketDataFreshnessChanged,
)
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.desktop.instrument_selection_panel import InstrumentSelectionPanel
from tfx_quant.desktop.login_dialog import LoginDialog
from tfx_quant.desktop.market_data_panel import MarketDataPanel
from tfx_quant.infrastructure.yuanta import login_preferences

_BG = wx.Colour(15, 23, 42)
_TEXT = wx.Colour(203, 213, 225)


class ReadinessFrame(wx.Frame):
    def __init__(self, parent: wx.Window | None, services: ServiceContainer) -> None:
        super().__init__(parent, title="TfxQuant", size=(1180, 760))
        self._services = services
        panel = wx.Panel(self)
        panel.SetBackgroundColour(_BG)
        root = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        brand = wx.StaticText(panel, label="TfxQuant")
        brand.SetForegroundColour(wx.WHITE)
        brand.SetFont(brand.GetFont().Bold().Scale(1.6))
        top.Add(brand, 0, wx.ALIGN_CENTER_VERTICAL)
        top.AddStretchSpacer()
        self._login_state = wx.StaticText(panel, label="未登入 · 行情仍會自動更新")
        self._login_state.SetForegroundColour(_TEXT)
        top.Add(self._login_state, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._login_button = wx.Button(panel, label="登入")
        self._login_button.Bind(wx.EVT_BUTTON, self._on_login)
        top.Add(self._login_button, 0)
        root.Add(top, 0, wx.EXPAND | wx.ALL, 20)

        self._selector = InstrumentSelectionPanel(panel, services)
        self._selector.SetBackgroundColour(_BG)
        for child in self._selector.GetChildren():
            if isinstance(child, wx.StaticText):
                child.SetForegroundColour(_TEXT)
        root.Add(self._selector, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)

        self._market = MarketDataPanel(panel, services)
        root.Add(self._market, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)
        panel.SetSizer(root)

        self._unsubscribers = [
            services.event_coordinator.subscribe(BrokerLoginSucceeded, self._on_login_succeeded),
            services.event_coordinator.subscribe(BrokerSessionReady, self._on_session_ready),
            services.event_coordinator.subscribe(BrokerLoggedOut, self._on_event),
            services.event_coordinator.subscribe(InstrumentSwitchCompleted, self._on_event),
            services.event_coordinator.subscribe(BarClosed, self._on_event),
            services.event_coordinator.subscribe(BarBackfillCompleted, self._on_event),
            services.event_coordinator.subscribe(MarketDataFreshnessChanged, self._on_event),
        ]
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()
        self._refresh()

    def _on_login(self, _event: wx.CommandEvent) -> None:
        if self._services.broker_session.capabilities.is_session_ready:
            self._services.broker_session.stop()
            return
        dialog = LoginDialog(self, self._services)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _on_login_succeeded(self, event: BrokerLoginSucceeded) -> None:
        # Deliberately no account switcher: use the remembered account when present,
        # otherwise deterministically select the first account returned by Yuanta.
        if self._services.broker_session.selected_account is None and event.accounts:
            self._services.broker_session.select_account(event.accounts[0])
        wx.CallAfter(self._refresh)

    def _on_session_ready(self, event: BrokerSessionReady) -> None:
        login_preferences.save_remembered_account_no(event.account.account_no)
        wx.CallAfter(self._refresh)

    def _on_event(self, _event: object) -> None:
        wx.CallAfter(self._refresh)

    def _refresh(self) -> None:
        ready = self._services.broker_session.capabilities.is_session_ready
        self._login_state.SetLabel("已登入" if ready else "未登入 · 行情仍會自動更新")
        self._login_button.SetLabel("登出" if ready else "登入")
        self._selector.refresh()
        self._market.refresh()

    def _on_close(self, event: wx.CloseEvent) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        event.Skip()
