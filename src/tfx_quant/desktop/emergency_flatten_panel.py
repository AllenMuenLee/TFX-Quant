"""Feature 10 emergency-flatten UI — the high-priority, operator-confirmed 緊急平倉
control. This panel only re-queries (via `PositionReconciliationService`, the same
fresh-query path Feature 08's manual sync uses), displays the result, and — only after
an explicit confirm click bound to that exact snapshot — calls
`application.risk.risk_supervisor.RiskSupervisor.trigger_emergency_flatten`. It never
calls the broker directly, never assembles its own reversal/close order, and never
clears its alert banner except when a fresh broker query (`EodFlattenCompleted`)
independently confirms the position is exactly zero.
"""

from __future__ import annotations

import wx

from tfx_quant.application.events.events import (
    EodFlattenCompleted,
    EodFlattenPausedSafe,
    EodFlattenWorkflowStarted,
    StartupPositionSafetyPauseTriggered,
)
from tfx_quant.application.position_reconciliation.errors import PositionReconciliationError
from tfx_quant.application.risk.errors import RiskSupervisorError, StaleEmergencyConfirmationError
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.position_reconciliation import ReconciliationRecord
from tfx_quant.domain.risk import EodFlattenWorkflowId, EodFlattenWorkflowState
from tfx_quant.telemetry.masking import mask_account

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_ALERT_COLOUR = wx.Colour(220, 38, 38)
_NORMAL_TEXT_COLOUR = wx.Colour(203, 213, 225)


class EmergencyFlattenPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services
        self._pending_record: ReconciliationRecord | None = None
        self._active_workflow_id: EodFlattenWorkflowId | None = None
        self._last_target: tuple[TradingAccount, Instrument, ContractMonth] | None = None

        outer = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(self, label="緊急平倉")
        header.SetFont(header.GetFont().Bold())
        outer.Add(header, 0, wx.BOTTOM, 4)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self._start_button = wx.Button(self, label="緊急平倉（重新查詢）")
        self._start_button.SetBackgroundColour(_ALERT_COLOUR)
        self._start_button.SetForegroundColour(wx.WHITE)
        self._start_button.Bind(wx.EVT_BUTTON, self._on_start)
        button_row.Add(self._start_button, 0, wx.RIGHT, 8)

        self._confirm_button = wx.Button(self, label="確認緊急平倉")
        self._confirm_button.Bind(wx.EVT_BUTTON, self._on_confirm)
        self._confirm_button.Disable()
        button_row.Add(self._confirm_button, 0)
        outer.Add(button_row, 0, wx.BOTTOM, 4)

        self._status = wx.StaticText(self, label="")
        outer.Add(self._status, 0)

        self.SetSizer(outer)

        self._unsubscribers = [
            services.event_coordinator.subscribe(
                EodFlattenWorkflowStarted, self._on_workflow_started
            ),
            services.event_coordinator.subscribe(EodFlattenPausedSafe, self._on_workflow_paused),
            services.event_coordinator.subscribe(EodFlattenCompleted, self._on_workflow_completed),
            services.event_coordinator.subscribe(
                StartupPositionSafetyPauseTriggered, self._on_startup_pause
            ),
        ]
        self._recover_active_workflow()

    def close(self) -> None:
        """Must be called by the owning frame on shutdown (see `readiness_frame.py`'s
        `_on_close`) — this panel subscribes to events independently of the frame's own
        `refresh()` polling, so it needs its own explicit unsubscribe."""
        for unsubscribe in self._unsubscribers:
            unsubscribe()

    # -- Current target -------------------------------------------------------------

    def _current_target(self) -> tuple[TradingAccount, Instrument, ContractMonth] | None:
        selection = self._services.instrument_selection.current
        account = self._services.broker_session.selected_account
        if selection is None or account is None:
            return None
        return account, selection.instrument, selection.contract

    def _recover_active_workflow(self) -> None:
        """Shows an already-active workflow's state immediately (e.g. after a restart
        mid-`PAUSED_SAFE`) rather than waiting for a new event that may never come."""
        target = self._current_target()
        self._last_target = target
        if target is None:
            return
        account, instrument, contract = target
        record = self._services.risk_supervisor.active_workflow_for(account, instrument, contract)
        if record is None:
            return
        self._active_workflow_id = record.workflow_id
        if record.state is EodFlattenWorkflowState.PAUSED_SAFE:
            self._show_alert(
                f"平倉尚未完成（{record.pause_reason or record.state.value}），"
                "請按「緊急平倉」重新查詢並確認"
            )
        else:
            self._status.SetLabel(f"平倉 workflow 進行中（狀態：{record.state.value}）")

    # -- Step 1: 重新查詢並顯示 -------------------------------------------------------

    def _on_start(self, _event: wx.CommandEvent) -> None:
        try:
            record = self._services.reconciliation_service.request_manual_requery()
        except PositionReconciliationError as exc:
            self._pending_record = None
            self._confirm_button.Disable()
            self._status.SetLabel(str(exc))
            return
        if record is None:
            self._pending_record = None
            self._confirm_button.Disable()
            self._status.SetLabel("尚未選定商品／契約或帳號，無法查詢")
            return
        self._pending_record = record
        self._render_pending()

    def _render_pending(self) -> None:
        record = self._pending_record
        if record is None:
            return
        masked_account = mask_account(record.account.account_no)
        lines = [
            f"帳號 {masked_account}｜{record.instrument.display_name_zh}｜"
            f"完整契約 {record.contract.code}",
        ]
        if record.query_succeeded and record.actual_net is not None:
            lines.append(f"元大實際持倉 {record.actual_net.lots} 口")
        else:
            lines.append(f"查詢失敗：{record.query_error}")
        lines.append(f"活動／未知委託：{record.active_or_unknown_order_count} 筆")
        if record.broker_snapshot_at is not None:
            lines.append(f"券商快照時間：{record.broker_snapshot_at.value.strftime(_TIME_FORMAT)}")
        lines.append("請核對以上帳號、商品、契約及口數後按「確認緊急平倉」")
        self._status.SetLabel("\n".join(lines))
        self._confirm_button.Enable(record.query_succeeded and record.actual_net is not None)

    # -- Step 2: 確認緊急平倉 ---------------------------------------------------------

    def _on_confirm(self, _event: wx.CommandEvent) -> None:
        record = self._pending_record
        if record is None or record.actual_net is None:
            return
        try:
            result = self._services.risk_supervisor.trigger_emergency_flatten(
                account=record.account,
                instrument=record.instrument,
                contract=record.contract,
                confirmed_net=record.actual_net,
            )
        except StaleEmergencyConfirmationError:
            self._pending_record = None
            self._confirm_button.Disable()
            self._status.SetLabel("持倉自上次查詢後已變動，請按「緊急平倉」重新查詢後再次確認")
            return
        except RiskSupervisorError as exc:
            self._status.SetLabel(str(exc))
            return

        self._active_workflow_id = result.workflow_id
        self._pending_record = None
        self._confirm_button.Disable()
        self._show_alert(f"平倉 workflow 已啟動（狀態：{result.state.value}）")

    # -- Workflow-progress events (any trigger source, not just this panel's own) --

    def _matches_current_target(self, instrument: Instrument, contract: ContractMonth) -> bool:
        target = self._current_target()
        return target is not None and (instrument, contract) == (target[1], target[2])

    def _on_workflow_started(self, event: EodFlattenWorkflowStarted) -> None:
        if not self._matches_current_target(event.instrument, event.contract):
            return
        self._active_workflow_id = event.workflow_id
        wx.CallAfter(
            self._status.SetLabel, f"平倉 workflow 已啟動（觸發來源：{event.trigger.value}）"
        )

    def _on_workflow_paused(self, event: EodFlattenPausedSafe) -> None:
        if event.workflow_id != self._active_workflow_id:
            return
        wx.CallAfter(self._show_alert, f"平倉未完成：{event.reason}（請重新查詢並確認緊急平倉）")

    def _on_workflow_completed(self, event: EodFlattenCompleted) -> None:
        if event.workflow_id != self._active_workflow_id:
            return
        self._active_workflow_id = None
        wx.CallAfter(self._clear_alert, "平倉已完成，券商查詢已確認持倉為零")

    def _on_startup_pause(self, event: StartupPositionSafetyPauseTriggered) -> None:
        if not self._matches_current_target(event.instrument, event.contract):
            return
        wx.CallAfter(
            self._show_alert,
            f"程式啟動時偵測到既有持倉（{event.net.lots} 口），已安全暫停，請執行緊急平倉",
        )

    def _show_alert(self, message: str) -> None:
        self._status.SetLabel(f"⚠ {message}")
        self._status.SetForegroundColour(_ALERT_COLOUR)

    def _clear_alert(self, message: str) -> None:
        self._status.SetLabel(message)
        self._status.SetForegroundColour(wx.NullColour)

    def refresh(self) -> None:
        target = self._current_target()
        if target == self._last_target:
            return
        # The monitored instrument/contract changed (Feature 03 switch) — any pending,
        # unconfirmed requery is now about the wrong contract and must never be
        # confirmed against it.
        self._pending_record = None
        self._active_workflow_id = None
        self._confirm_button.Disable()
        self._status.SetLabel("")
        self._status.SetForegroundColour(wx.NullColour)
        self._recover_active_workflow()
