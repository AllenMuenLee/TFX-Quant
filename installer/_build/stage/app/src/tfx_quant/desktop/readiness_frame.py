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
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.desktop.composition import ServiceContainer, start_test_env_quote_login
from tfx_quant.desktop.emergency_flatten_panel import EmergencyFlattenPanel
from tfx_quant.desktop.instrument_selection_panel import InstrumentSelectionPanel
from tfx_quant.desktop.log_viewer import LogViewerFrame
from tfx_quant.desktop.login_dialog import LoginDialog
from tfx_quant.desktop.market_data_panel import MarketDataPanel
from tfx_quant.desktop.reconciliation_panel import ReconciliationPanel
from tfx_quant.desktop.simulation_banner import SimulationBanner
from tfx_quant.desktop.trading_activity_panel import TradingActivityPanel
from tfx_quant.infrastructure.yuanta import login_preferences

_BG = wx.Colour(15, 23, 42)
_TEXT = wx.Colour(203, 213, 225)

_ENVIRONMENT_CHOICES: tuple[Environment, ...] = (Environment.TEST, Environment.PRODUCTION)
_ENVIRONMENT_LABELS = ("模擬下單（真實行情）", "正式下單（PRODUCTION）")
_PRODUCTION_SWITCH_CONFIRM = (
    "即將切換到「正式下單」執行環境。之後的委託將經由元大 API 送出到正式主機，\n"
    "可能影響真實帳戶與交易資料。確定要切換嗎？"
)


class ReadinessFrame(wx.Frame):
    def __init__(self, parent: wx.Window | None, services: ServiceContainer) -> None:
        title = "TfxQuant — 測試環境（真實行情・模擬下單）" if services.simulation else "TfxQuant"
        super().__init__(parent, title=title, size=(1180, 760))
        self._services = services
        panel = wx.Panel(self)
        panel.SetBackgroundColour(_BG)
        root = wx.BoxSizer(wx.VERTICAL)

        if services.simulation:
            root.Add(SimulationBanner(panel, services), 0, wx.EXPAND)

        top = wx.BoxSizer(wx.HORIZONTAL)
        brand_label = (
            "TfxQuant  [測試環境 — 真實行情・模擬下單]" if services.simulation else "TfxQuant"
        )
        brand = wx.StaticText(panel, label=brand_label)
        brand.SetForegroundColour(wx.WHITE)
        brand.SetFont(brand.GetFont().Bold().Scale(1.6))
        top.Add(brand, 0, wx.ALIGN_CENTER_VERTICAL)
        top.AddSpacer(16)
        self._environment_radio = wx.RadioBox(
            panel, label="執行環境", choices=list(_ENVIRONMENT_LABELS), style=wx.RA_SPECIFY_COLS
        )
        self._environment_radio.SetSelection(
            _ENVIRONMENT_CHOICES.index(services.settings.environment)
        )
        self._environment_radio.Bind(wx.EVT_RADIOBOX, self._on_environment_changed)
        for child in self._environment_radio.GetChildren():
            child.SetForegroundColour(_TEXT)
        self._environment_radio.SetForegroundColour(_TEXT)
        top.Add(self._environment_radio, 0, wx.ALIGN_CENTER_VERTICAL)
        top.AddStretchSpacer()
        self._login_state = wx.StaticText(panel, label="交易與行情未登入")
        self._login_state.SetForegroundColour(_TEXT)
        top.Add(self._login_state, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self._log_button = wx.Button(panel, label="查看所有日誌")
        self._log_button.Bind(wx.EVT_BUTTON, self._on_show_logs)
        top.Add(self._log_button, 0, wx.RIGHT, 8)
        self._login_button = wx.Button(panel, label="行情登入" if services.simulation else "登入")
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

        self._activity = TradingActivityPanel(panel, services)
        self._activity.SetBackgroundColour(_BG)
        for child in self._activity.GetChildren():
            if isinstance(child, wx.StaticText):
                child.SetForegroundColour(_TEXT)
        root.Add(self._activity, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)

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

    # -- environment selector --------------------------------------------------------

    def _on_environment_changed(self, _event: wx.CommandEvent) -> None:
        chosen = _ENVIRONMENT_CHOICES[self._environment_radio.GetSelection()]
        if chosen is self._services.settings.environment:
            return
        app = wx.GetApp()
        blocked = app.can_switch_environment()
        if blocked is not None:
            wx.MessageBox(blocked, "無法切換執行環境", wx.OK | wx.ICON_WARNING, self)
            self.sync_environment_selector(self._services.settings.environment)
            return
        if chosen is Environment.PRODUCTION:
            confirmed = (
                wx.MessageBox(
                    _PRODUCTION_SWITCH_CONFIRM, "切換到正式環境", wx.YES_NO | wx.ICON_WARNING, self
                )
                == wx.YES
            )
            if not confirmed:
                self.sync_environment_selector(self._services.settings.environment)
                return
        app.switch_environment(chosen)

    def sync_environment_selector(self, environment: Environment) -> None:
        self._environment_radio.SetSelection(_ENVIRONMENT_CHOICES.index(environment))

    def open_login_dialog(self) -> None:
        """Public — `TfxQuantApp` calls this on the freshly rebuilt frame after an
        environment switch so the operator's login flow continues without a second click."""
        self._on_login(wx.CommandEvent())

    def _on_login(self, _event: wx.CommandEvent) -> None:
        if self._services.simulation:
            self._prompt_for_test_env_quote_login()
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

    def _prompt_for_test_env_quote_login(self) -> None:
        from tfx_quant.desktop.quote_login_dialog import QuoteLoginDialog

        remembered = login_preferences.load().remembered_user_id
        dialog = QuoteLoginDialog(self, remembered_user_id=remembered)
        try:
            if dialog.ShowModal() != wx.ID_OK or dialog.credentials is None:
                return
            user_id, password = dialog.credentials
        finally:
            dialog.Destroy()
        login_preferences.save_remembered_user_id(user_id)
        try:
            start_test_env_quote_login(self._services, user_id, password)
        except Exception as exc:
            wx.MessageBox(str(exc), "行情登入失敗", wx.OK | wx.ICON_ERROR, self)
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
            quote_ready = self._services.quote_runtime.state is QuoteConnectionState.LOGGED_ON
            self._login_state.SetLabel(
                "交易：本機模擬　·　行情：元大即時（真實）"
                f"{'　已登入' if quote_ready else '　未登入'}"
            )
            self._login_button.SetLabel("行情登入")
            self._selector.refresh()
            self._market.refresh()
            return
        # The two logins are independent (see composition.readiness_rows): the trading
        # OCX and the quote OCX authenticate separately, so this label must not report
        # 行情 from the broker session alone.
        trading_ready = self._services.broker_session.capabilities.is_session_ready
        quote_ready = self._services.quote_runtime.state is QuoteConnectionState.LOGGED_ON
        self._login_state.SetLabel(
            f"交易{'已' if trading_ready else '未'}登入 · 行情{'已' if quote_ready else '未'}登入"
        )
        self._login_button.SetLabel("登出" if trading_ready else "登入")
        self._selector.refresh()
        self._market.refresh()

    def _on_close(self, event: wx.CloseEvent) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        event.Skip()
