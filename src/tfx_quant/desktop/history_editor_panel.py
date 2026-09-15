"""Manual hourly OHLCV editor shared by every trading environment."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import wx

from tfx_quant.desktop.quote_runtime import QuoteRuntime
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


class HistoryEditorPanel(wx.Panel):
    def __init__(
        self, parent: wx.Window, runtime: QuoteRuntime, on_saved: Callable[[], None]
    ) -> None:
        super().__init__(parent)
        self.SetForegroundColour(wx.Colour(203, 213, 225))
        self._runtime, self._on_saved = runtime, on_saved
        root = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(self, label="每小時歷史資料（模擬與正式模式共用）")
        root.Add(title, 0, wx.BOTTOM, 6)
        hint = wx.StaticText(
            self,
            label=(
                "使用目前選擇的商品與契約；時間為臺北時間的 K 棒收盤時間。"
                "例如 08:45–09:45 顯示為 09:45；箭頭會跳過休市時段並銜接日盤與夜盤。"
                "僅可輸入已收盤 K 棒。"
            ),
        )
        root.Add(hint, 0, wx.BOTTOM, 6)
        grid = wx.FlexGridSizer(rows=2, cols=6, vgap=4, hgap=6)
        labels = ("收盤時間（年-月-日 時:分）", "開盤價", "收盤價", "最高價", "最低價", "成交量")
        for label in labels:
            grid.Add(wx.StaticText(self, label=label))
        self._close = wx.TextCtrl(
            self, value=datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d 09:45"), size=(190, -1)
        )
        timestamp_row = wx.BoxSizer(wx.HORIZONTAL)
        previous = wx.Button(self, label="◀", style=wx.BU_EXACTFIT)
        previous.SetToolTip("上一根 K 棒（跨日盤／夜盤）")
        previous.Bind(wx.EVT_BUTTON, lambda _event: self._step(-1))
        following = wx.Button(self, label="▶", style=wx.BU_EXACTFIT)
        self._following = following
        following.SetToolTip("下一根 K 棒（跨日盤／夜盤）")
        following.Bind(wx.EVT_BUTTON, lambda _event: self._step(1))
        timestamp_row.Add(previous, 0, wx.RIGHT, 4)
        timestamp_row.Add(self._close, 0, wx.RIGHT, 4)
        timestamp_row.Add(following)
        grid.Add(timestamp_row)
        self._fields = {}
        for key in ("open", "close", "high", "low", "volume"):
            control = wx.TextCtrl(self, size=(90, -1))
            self._fields[key] = control
            grid.Add(control)
        root.Add(grid, 0, wx.BOTTOM, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("載入此時段", self._load),
            ("儲存／更新此時段", self._save),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            if handler == self._save:
                self._save_button = button
            row.Add(button, 0, wx.RIGHT, 6)
        self._status = wx.StaticText(self, label="手動資料會保留，修改時會記錄修訂歷程。")
        row.Add(self._status, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(row)
        self.SetSizer(root)
        try:
            latest = runtime.latest_editable_history_close()
            self._close.ChangeValue(latest.value.strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            pass  # No selected contract yet; validation keeps editing disabled.
        self._close.Bind(wx.EVT_TEXT, lambda _event: self._update_editability())
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _event: self._update_editability(), self._timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._timer.Start(1000)
        self._update_editability()

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() is self:
            self._timer.Stop()
        event.Skip()

    def _update_editability(self) -> None:
        try:
            close = self._timestamp()
            self._runtime.validate_history_close(close)
        except ValueError as exc:
            editable = False
            self._status.SetLabel(str(exc))
        else:
            editable = True
            if not self._save_button.IsEnabled():
                self._status.SetLabel("此 K 棒已收盤，可載入或編輯歷史資料。")
        self._save_button.Enable(editable)
        for control in self._fields.values():
            control.Enable(editable)
        can_advance = False
        if editable:
            try:
                following = self._runtime.adjacent_history_close(close, 1)
                self._runtime.validate_history_close(following)
                can_advance = True
            except ValueError:
                pass
        self._following.Enable(can_advance)
        self.Layout()

    def _timestamp(self) -> Timestamp:
        return Timestamp(
            datetime.strptime(self._close.GetValue().strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=TAIPEI_TZ
            )
        )

    def _step(self, direction: int) -> None:
        try:
            close = self._runtime.adjacent_history_close(self._timestamp(), direction)
            self._runtime.validate_history_close(close)
            self._close.SetValue(close.value.strftime("%Y-%m-%d %H:%M"))
            self._load(None)
        except Exception as exc:
            self._status.SetLabel(str(exc))
        self.Layout()

    def _load(self, _event: wx.CommandEvent | None) -> None:
        try:
            self._runtime.validate_history_close(self._timestamp())
            start = self._runtime.history_start_for_close(self._timestamp())
            record = self._runtime.history_hour(start)
            for key, control in self._fields.items():
                value = getattr(record.bar, key) if record else None
                control.SetValue(
                    "" if value is None else str(value if key == "volume" else value.amount)
                )
            self._status.SetLabel(
                "此時段尚無資料，請輸入價格與成交量。"
                if record is None
                else f"已載入第 {record.revision} 次修訂。"
            )
        except Exception as exc:
            self._status.SetLabel(str(exc))
        self.Layout()

    def _save(self, _event: wx.CommandEvent) -> None:
        try:
            record = self._runtime.save_history_hour(
                self._runtime.history_start_for_close(self._timestamp()),
                **{key: field.GetValue() for key, field in self._fields.items()},
            )
        except Exception as exc:
            self._status.SetLabel(f"未儲存：{exc}")
        else:
            self._status.SetLabel(f"已儲存至共用資料庫（第 {record.revision} 次修訂）。")
            self._on_saved()
        self.Layout()
