"""ConnectivityPanel — Feature 09's per-channel connectivity health, safe-pause reason,
and reconnect-status display.

Embedded in `ReadinessFrame`, mirroring `MarketDataPanel`'s embedding: purely a display
surface (no order-sending control) plus the one operator action this feature's
implementation prompt explicitly calls for ("允許使用者停止" the in-progress reconnect
backoff) — a "停止重連" button. `ReadinessFrame` owns the event subscriptions and calls
this panel's `refresh()`, same convention as every other embedded panel.
"""

from __future__ import annotations

import wx

from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.connectivity import ChannelHealth, ChannelId

_CHANNEL_LABEL_ZH = {
    ChannelId.LOGIN: "登入 (Login)",
    ChannelId.MARKET_DATA: "行情 (Market Data)",
    ChannelId.TRADE: "交易 (Trade)",
    ChannelId.ORDER_REPORTS: "回報 (Order Reports)",
    ChannelId.QUERIES: "查詢 (Queries)",
}


def _format_channel(health: ChannelHealth) -> str:
    status = "OK" if health.is_healthy else "--"
    last_message = (
        "無"
        if health.last_message_at is None
        else health.last_message_at.value.strftime("%H:%M:%S")
    )
    latency = "無" if health.latency_ms is None else f"{health.latency_ms:.0f}ms"
    parts = [
        f"[{status}] {_CHANNEL_LABEL_ZH[health.channel]}",
        "已連線" if health.connected else "未連線",
        "STALE" if health.is_stale else "FRESH",
        f"最後訊息 {last_message}",
        f"延遲 {latency}",
    ]
    if health.last_error is not None:
        parts.append(f"錯誤：{health.last_error}")
    return "｜".join(parts)


class ConnectivityPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services

        sizer = wx.BoxSizer(wx.VERTICAL)

        self._channel_list = wx.ListBox(self, size=(-1, 100))
        sizer.Add(self._channel_list, 0, wx.EXPAND | wx.ALL, 6)

        self._pause_label = wx.StaticText(self, label="（本次連線尚未發生安全暫停）")
        sizer.Add(self._pause_label, 0, wx.ALL, 6)

        reconnect_row = wx.BoxSizer(wx.HORIZONTAL)
        self._reconnect_label = wx.StaticText(self, label="（未在重連中）")
        reconnect_row.Add(self._reconnect_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        self._stop_reconnect_button = wx.Button(self, label="停止重連 (Stop Reconnect)")
        self._stop_reconnect_button.Bind(wx.EVT_BUTTON, self._on_stop_reconnect)
        reconnect_row.Add(self._stop_reconnect_button, 0, wx.ALL, 6)
        sizer.Add(reconnect_row, 0)

        self.SetSizer(sizer)
        self._refresh()

    def refresh(self) -> None:
        """Called by `ReadinessFrame` on every broker/market-data/connectivity event —
        see module docstring for why the subscriptions live there."""
        self._refresh()

    def _on_stop_reconnect(self, _event: wx.CommandEvent) -> None:
        self._services.connectivity_monitor.cancel_reconnect()
        self._refresh()

    def _refresh(self) -> None:
        monitor = self._services.connectivity_monitor
        health_by_channel = monitor.all_channel_health()
        self._channel_list.Set(
            [_format_channel(health_by_channel[channel]) for channel in ChannelId]
        )

        record = monitor.current_pause()
        if record is None:
            self._pause_label.SetLabel("（本次連線尚未發生安全暫停）")
        else:
            reconciled = "已完成重連核對" if record.reconciled else "尚待重連核對"
            expected_net = "無" if record.expected_net_lots is None else record.expected_net_lots
            self._pause_label.SetLabel(
                f"安全暫停原因：{record.reason.value}｜通道：{record.channel.value}｜"
                f"偵測時間 {record.detected_at.value.strftime('%H:%M:%S')}｜"
                f"暫停生效 {record.effective_at.value.strftime('%H:%M:%S')}｜"
                f"當時活動委託 {record.active_order_count} 筆｜"
                f"當時預期持倉 {expected_net}｜"
                f"{reconciled}｜詳情：{record.detail}"
            )

        if monitor.is_reconnecting:
            attempt = monitor.reconnect_attempt_count
            self._reconnect_label.SetLabel(f"重連中…（第 {attempt} 次嘗試）")
            self._stop_reconnect_button.Enable(True)
        else:
            self._reconnect_label.SetLabel("（未在重連中）")
            self._stop_reconnect_button.Enable(False)
