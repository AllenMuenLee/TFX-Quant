"""ReadinessFrame — the startup diagnostics + session-control screen.

Shows per-module readiness (including the five independent `SessionCapabilities`
flags) and lets the operator start/stop the Yuanta session and pick an account when
login returns more than one. No order-sending control of any kind exists in this
window (or anywhere else in this feature) — connecting the broker session and
selecting an account are session-lifecycle actions, not order submission, so they stay
within `docs/adr/0002-ui-framework-and-com-hosting.md`'s "no order-sending UI"
constraint.

Broker events arrive on `EventCoordinator`'s own consumer thread (see
`application/events/event_coordinator.py`), never the wx UI thread — every handler
here hops back via `wx.CallAfter` before touching any wx control, since wxPython
widgets may only be touched from the UI thread.
"""

from __future__ import annotations

import wx

from tfx_quant.application.events.events import (
    BarClosed,
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginSucceeded,
    BrokerSessionReady,
    InstrumentSwitchCompleted,
    MarketDataFreshnessChanged,
    MarketDataGapCleared,
    MarketDataGapDetected,
    MarketDataTickReceived,
)
from tfx_quant.desktop.composition import ServiceContainer, compute_readiness
from tfx_quant.desktop.instrument_selection_panel import InstrumentSelectionPanel
from tfx_quant.desktop.market_data_panel import MarketDataPanel
from tfx_quant.domain.account import TradingAccount


def _account_label(account: TradingAccount) -> str:
    return f"{account.branch_id}-{account.account_no}-{account.sub_account}"


class ReadinessFrame(wx.Frame):
    def __init__(self, parent: wx.Window | None, services: ServiceContainer) -> None:
        super().__init__(parent, title="TfxQuant — Startup Diagnostics")
        self._services = services

        self._panel = wx.Panel(self)
        self._sizer = wx.BoxSizer(wx.VERTICAL)

        settings = services.settings
        header = (
            f"Account: {settings.account_alias}\n"
            f"Environment: {settings.environment.value}\n"
            f"Instrument: {settings.selected_instrument.value}\n"
            f"Contract selection: {settings.contract_selection_mode.value}\n"
            f"Max net lots: {settings.max_net_lots}"
        )
        self._sizer.Add(wx.StaticText(self._panel, label=header), 0, wx.ALL, 10)
        self._sizer.Add(wx.StaticLine(self._panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self._readiness_sizer = wx.BoxSizer(wx.VERTICAL)
        self._sizer.Add(self._readiness_sizer, 0, wx.EXPAND)
        self._sizer.Add(wx.StaticLine(self._panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        session_row = wx.BoxSizer(wx.HORIZONTAL)
        self._connect_button = wx.Button(self._panel, label="連線 (Connect)")
        self._connect_button.Bind(wx.EVT_BUTTON, self._on_connect)
        session_row.Add(self._connect_button, 0, wx.ALL, 6)

        self._disconnect_button = wx.Button(self._panel, label="登出 (Disconnect)")
        self._disconnect_button.Bind(wx.EVT_BUTTON, self._on_disconnect)
        session_row.Add(self._disconnect_button, 0, wx.ALL, 6)
        self._sizer.Add(session_row, 0)

        account_row = wx.BoxSizer(wx.HORIZONTAL)
        account_row.Add(
            wx.StaticText(self._panel, label="帳號 (Account):"),
            0,
            wx.ALL | wx.ALIGN_CENTER_VERTICAL,
            6,
        )
        self._account_choice = wx.Choice(self._panel, choices=[])
        account_row.Add(self._account_choice, 0, wx.ALL, 6)
        self._select_account_button = wx.Button(self._panel, label="選擇帳號 (Select)")
        self._select_account_button.Bind(wx.EVT_BUTTON, self._on_select_account)
        account_row.Add(self._select_account_button, 0, wx.ALL, 6)
        self._sizer.Add(account_row, 0)
        self._sizer.Add(wx.StaticLine(self._panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self._instrument_selection_panel = InstrumentSelectionPanel(self._panel, services)
        self._sizer.Add(self._instrument_selection_panel, 0, wx.EXPAND | wx.ALL, 6)
        self._sizer.Add(wx.StaticLine(self._panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self._market_data_panel = MarketDataPanel(self._panel, services)
        self._sizer.Add(self._market_data_panel, 0, wx.EXPAND | wx.ALL, 6)

        self._panel.SetSizer(self._sizer)
        self._refresh()
        self.Fit()

        self._unsubscribers = [
            services.event_coordinator.subscribe(BrokerLoginSucceeded, self._on_broker_event),
            services.event_coordinator.subscribe(BrokerCapabilitiesChanged, self._on_broker_event),
            services.event_coordinator.subscribe(BrokerSessionReady, self._on_broker_event),
            services.event_coordinator.subscribe(BrokerLoggedOut, self._on_broker_event),
            services.event_coordinator.subscribe(InstrumentSwitchCompleted, self._on_broker_event),
            services.event_coordinator.subscribe(MarketDataTickReceived, self._on_broker_event),
            services.event_coordinator.subscribe(BarClosed, self._on_broker_event),
            services.event_coordinator.subscribe(
                MarketDataFreshnessChanged, self._on_broker_event
            ),
            services.event_coordinator.subscribe(MarketDataGapDetected, self._on_broker_event),
            services.event_coordinator.subscribe(MarketDataGapCleared, self._on_broker_event),
        ]
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _on_broker_event(self, _event: object) -> None:
        wx.CallAfter(self._refresh)

    def _on_connect(self, _wx_event: wx.CommandEvent) -> None:
        self._services.broker_session.start()

    def _on_disconnect(self, _wx_event: wx.CommandEvent) -> None:
        self._services.broker_session.stop()

    def _on_select_account(self, _wx_event: wx.CommandEvent) -> None:
        index = self._account_choice.GetSelection()
        if index == wx.NOT_FOUND:
            return
        accounts = self._services.broker_session.accounts
        if index >= len(accounts):
            return
        self._services.broker_session.select_account(accounts[index])

    def _on_close(self, event: wx.CloseEvent) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        event.Skip()

    def _refresh(self) -> None:
        self._readiness_sizer.Clear(delete_windows=True)
        for label, ready in compute_readiness(self._services):
            mark = "OK" if ready else "--"
            self._readiness_sizer.Add(
                wx.StaticText(self._panel, label=f"[{mark}] {label}"), 0, wx.ALL, 6
            )

        accounts = self._services.broker_session.accounts
        self._account_choice.Set([_account_label(a) for a in accounts])
        selected = self._services.broker_session.selected_account
        if selected is not None and selected in accounts:
            self._account_choice.SetSelection(accounts.index(selected))

        self._instrument_selection_panel.refresh()
        self._market_data_panel.refresh()

        self._sizer.Layout()
        self._panel.SetSizerAndFit(self._sizer)
        self.Fit()
