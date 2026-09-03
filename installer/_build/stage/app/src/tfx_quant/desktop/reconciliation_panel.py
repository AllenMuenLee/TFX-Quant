"""Read-only status for automatic local-versus-broker position reconciliation."""

from __future__ import annotations

import wx

from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.position_reconciliation import DiscrepancyKind, ReconciliationRecord
from tfx_quant.telemetry.masking import mask_account

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ReconciliationPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services

        outer = wx.BoxSizer(wx.VERTICAL)
        header = wx.StaticText(self, label="自動持倉核對")
        header.SetFont(header.GetFont().Bold())
        outer.Add(header, 0, wx.BOTTOM, 4)
        self._status = wx.StaticText(self, label="等待自動核對")
        outer.Add(self._status, 0)
        self.SetSizer(outer)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._timer.Start(1000)
        self.refresh()

    def _on_timer(self, _event: wx.TimerEvent) -> None:
        self.refresh()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() is self:
            self._timer.Stop()
        event.Skip()

    def refresh(self) -> None:
        self._render(self._services.reconciliation_service.last_record)

    def _render(self, record: ReconciliationRecord | None) -> None:
        if record is None:
            self._status.SetLabel("等待登入後自動核對本機與線上持倉")
            return

        lines = [
            f"帳號 {mask_account(record.account.account_no)}｜{record.instrument.display_name_zh} "
            f"{record.contract.year:04d}-{record.contract.month:02d}",
            f"本機紀錄 {record.expected_net.lots} 口",
        ]
        if record.query_succeeded and record.actual_net is not None:
            lines.append(f"線上紀錄 {record.actual_net.lots} 口")
        else:
            lines.append(f"線上查詢失敗：{record.query_error}")
        if record.broker_snapshot_at is not None:
            lines.append(f"線上時間 {record.broker_snapshot_at.value.strftime(_TIME_FORMAT)}")
        if record.discrepancy is DiscrepancyKind.NONE and record.query_succeeded:
            lines.append("核對一致")
        else:
            lines.append(f"核對異常：{record.discrepancy.value}；交易已進入緊急暫停")
        self._status.SetLabel("\n".join(lines))
