"""Main desktop dashboard: market data first, broker login optional."""

from __future__ import annotations

import wx

from tfx_quant.application.events.events import (
    BarClosed,
    BrokerLoggedOut,
    BrokerLoginSucceeded,
    BrokerSessionReady,
    InstrumentSwitchCompleted,
    MarketDataFreshnessChanged,
)
from tfx_quant.application.ports.quote_gateway import QuoteConnectionState
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.desktop.emergency_flatten_panel import EmergencyFlattenPanel
from tfx_quant.desktop.instrument_selection_panel import InstrumentSelectionPanel
from tfx_quant.desktop.log_viewer import LogViewerFrame
from tfx_quant.desktop.login_dialog import LoginDialog
from tfx_quant.desktop.market_data_panel import MarketDataPanel
from tfx_quant.desktop.reconciliation_panel import ReconciliationPanel
from tfx_quant.infrastructure.yuanta import login_preferences

_BG = wx.Colour(15, 23, 42)
_TEXT = wx.Colour(203, 213, 225)


class ReadinessFrame(wx.Frame):
    def __init__(self, parent: wx.Window | None, services: ServiceContainer) -> None:
        title = "TfxQuant — SIMULATION / NO REAL ORDERS" if services.simulation else "TfxQuant"
        super().__init__(parent, title=title, size=(1180, 760))
        self._services = services
        panel = wx.Panel(self)
        panel.SetBackgroundColour(_BG)
        root = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        brand_label = "TfxQuant  [SIMULATION — FAKE ORDERS]" if services.simulation else "TfxQuant"
        brand = wx.StaticText(panel, label=brand_label)
        brand.SetForegroundColour(wx.WHITE)
        brand.SetFont(brand.GetFont().Bold().Scale(1.6))
        top.Add(brand, 0, wx.ALIGN_CENTER_VERTICAL)
        top.AddStretchSpacer()
        self._login_state = wx.StaticText(panel, label="交易與行情未登入")
        self._login_state.SetForegroundColour(_TEXT)
        top.Add(self._login_state, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._log_button = wx.Button(panel, label="查看所有日誌")
        self._log_button.Bind(wx.EVT_BUTTON, self._on_show_logs)
        top.Add(self._log_button, 0, wx.RIGHT, 8)
        self._login_button = wx.Button(panel, label="登入")
        self._mock_data_button: wx.Button | None = None
        if services.simulation:
            self._mock_data_button = wx.Button(panel, label="Use mock data")
            self._mock_data_button.Bind(wx.EVT_BUTTON, self._on_use_mock_data)
            top.Insert(top.GetItemCount() - 1, self._mock_data_button, 0, wx.RIGHT, 8)
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

        self._reconciliation = ReconciliationPanel(panel, services)
        self._reconciliation.SetBackgroundColour(_BG)
        for child in self._reconciliation.GetChildren():
            if isinstance(child, wx.StaticText):
                child.SetForegroundColour(_TEXT)
        root.Add(self._reconciliation, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)

        self._emergency_flatten = EmergencyFlattenPanel(panel, services)
        self._emergency_flatten.SetBackgroundColour(_BG)
        for child in self._emergency_flatten.GetChildren():
            if isinstance(child, wx.StaticText):
                child.SetForegroundColour(_TEXT)
        root.Add(self._emergency_flatten, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)

        panel.SetSizer(root)

        self._unsubscribers = [
            services.event_coordinator.subscribe(BrokerLoginSucceeded, self._on_login_succeeded),
            services.event_coordinator.subscribe(BrokerSessionReady, self._on_session_ready),
            services.event_coordinator.subscribe(BrokerLoggedOut, self._on_event),
            services.event_coordinator.subscribe(InstrumentSwitchCompleted, self._on_event),
            services.event_coordinator.subscribe(BarClosed, self._on_event),
            services.event_coordinator.subscribe(MarketDataFreshnessChanged, self._on_event),
        ]
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()
        self._refresh()

    def _on_show_logs(self, _event: wx.CommandEvent) -> None:
        LogViewerFrame(self).Show()

    def _on_login(self, _event: wx.CommandEvent) -> None:
        if self._services.simulation:
            self._prompt_for_real_quote_login()
            return
        if self._services.broker_session.capabilities.is_session_ready:
            self._services.broker_session.stop()
            self._services.quote_runtime.stop()
            return
        dialog = LoginDialog(self, self._services)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _on_use_mock_data(self, _event: wx.CommandEvent) -> None:
        control = self._services.simulation_market_data
        if control is None:
            return
        try:
            control.use_mock_data()
        except Exception as exc:
            wx.MessageBox(str(exc), "Mock data error", wx.OK | wx.ICON_ERROR, self)
        self._refresh()

    def _prompt_for_real_quote_login(self) -> None:
        control = self._services.simulation_market_data
        if control is None:
            return
        user_dialog = wx.TextEntryDialog(
            self, "Yuanta quote user ID", "Real quote data login"
        )
        try:
            if user_dialog.ShowModal() != wx.ID_OK:
                return
            user_id = user_dialog.GetValue()
        finally:
            user_dialog.Destroy()
        password_dialog = wx.TextEntryDialog(
            self,
            "Yuanta quote password",
            "Real quote data login",
            style=wx.OK | wx.CANCEL | wx.TE_PASSWORD,
        )
        try:
            if password_dialog.ShowModal() != wx.ID_OK:
                return
            password = password_dialog.GetValue()
        finally:
            password_dialog.Destroy()
        try:
            control.use_real_data(user_id, password)
        except Exception as exc:
            wx.MessageBox(str(exc), "Real quote login failed", wx.OK | wx.ICON_ERROR, self)
        self._refresh()

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
        if self._services.simulation:
            control = self._services.simulation_market_data
            source = "UNKNOWN" if control is None else control.source
            self._login_state.SetLabel(f"Trade: OFFLINE SIMULATOR | Data: {source}")
            self._login_button.SetLabel("Real quote login")
            self._selector.refresh()
            self._market.refresh()
            return
        # The two logins are independent (see composition.readiness_rows): the trading
        # OCX and the quote OCX authenticate separately, so this label must not report
        # 行情 from the broker session alone.
        trading_ready = self._services.broker_session.capabilities.is_session_ready
        quote_ready = self._services.quote_runtime.state is QuoteConnectionState.LOGGED_ON
        self._login_state.SetLabel(
            f"交易{'已' if trading_ready else '未'}登入"
            f" · 行情{'已' if quote_ready else '未'}登入"
        )
        self._login_button.SetLabel("登出" if trading_ready else "登入")
        self._selector.refresh()
        self._market.refresh()

    def _on_close(self, event: wx.CloseEvent) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        event.Skip()
