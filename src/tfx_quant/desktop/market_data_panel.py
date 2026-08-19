"""MarketDataPanel — Feature 04's forming/closed-bar and staleness display.

Embedded in `ReadinessFrame`, mirroring `InstrumentSelectionPanel`'s embedding: purely a
display surface (no order-sending control, matching this codebase's existing UI
constraint) with a `refresh()` the parent frame calls. `ReadinessFrame` — not this panel
— owns the market-data event subscriptions (`BarClosed`/`MarketDataTickReceived`/
`MarketDataFreshnessChanged`/`MarketDataGapDetected`/`MarketDataGapCleared`) and their
`wx.CallAfter` hop off the `EventCoordinator` consumer thread, same as it already does
for broker session events — keeping subscription lifecycle in one place (the frame's
existing `_on_close` teardown) rather than duplicating it per embedded panel.
"""

from __future__ import annotations

from datetime import date, timedelta

import wx
import wx.adv

from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.bar_record import BarRecord

_CANDLE_LABEL_ZH = {
    CandleColor.RED: "紅",
    CandleColor.BLACK: "黑",
    CandleColor.DOJI: "十字",
}
_RECENT_BARS_SHOWN = 10
_HISTORY_ROWS_SHOWN = 200
_DEFAULT_HISTORY_QUERY_DAYS = 7


def _format_bar(bar: Bar) -> str:
    label = bar.start.value.strftime("%m/%d %H:%M")
    color = _CANDLE_LABEL_ZH[bar.candle_color]
    return (
        f"{label}｜開 {bar.open.amount} 高 {bar.high.amount} 低 {bar.low.amount} "
        f"收 {bar.close.amount}｜量 {bar.volume}｜{color}"
    )


def _format_record(record: BarRecord) -> str:
    completeness = "缺口後首根，前段資料可能不完整" if record.is_gap_recovery else "完整"
    return (
        f"{_format_bar(record.bar)}｜來源：本軟體自行聚合（非元大官方歷史 K 棒）"
        f"｜完整性：{completeness}"
    )


def _wx_date_to_date(value: wx.DateTime) -> date:
    return date(value.GetYear(), value.GetMonth() + 1, value.GetDay())


def _date_to_wx_date(value: date) -> wx.DateTime:
    wx_date = wx.DateTime()
    wx_date.Set(value.day, value.month - 1, value.year)
    return wx_date


class MarketDataPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services

        sizer = wx.BoxSizer(wx.VERTICAL)

        status_row = wx.BoxSizer(wx.HORIZONTAL)
        status_row.Add(
            wx.StaticText(self, label="行情狀態："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._status_label = wx.StaticText(self, label="（尚未選擇契約）")
        status_row.Add(self._status_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer.Add(status_row, 0)

        forming_row = wx.BoxSizer(wx.HORIZONTAL)
        forming_row.Add(
            wx.StaticText(self, label="目前 K 棒："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._forming_label = wx.StaticText(self, label="（尚無資料）")
        forming_row.Add(self._forming_label, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        sizer.Add(forming_row, 0)

        sizer.Add(wx.StaticText(self, label="最近收盤 K 棒："), 0, wx.ALL, 6)
        self._recent_list = wx.ListBox(self, size=(-1, 140))
        sizer.Add(self._recent_list, 0, wx.EXPAND | wx.ALL, 6)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(
            wx.StaticText(self, label="歷史 K 棒（本機自行收錄，最多滾動兩個月）："), 0, wx.ALL, 6
        )
        self._history_range_label = wx.StaticText(self, label="（尚未收錄任何資料）")
        sizer.Add(self._history_range_label, 0, wx.ALL, 6)

        history_query_row = wx.BoxSizer(wx.HORIZONTAL)
        history_query_row.Add(
            wx.StaticText(self, label="起："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        default_end = wx.DateTime.Now()
        default_start_date = date.today() - timedelta(days=_DEFAULT_HISTORY_QUERY_DAYS)
        default_start = _date_to_wx_date(default_start_date)
        self._history_start_picker = wx.adv.DatePickerCtrl(self)
        self._history_start_picker.SetValue(default_start)
        history_query_row.Add(self._history_start_picker, 0, wx.ALL, 6)
        history_query_row.Add(
            wx.StaticText(self, label="迄："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._history_end_picker = wx.adv.DatePickerCtrl(self)
        self._history_end_picker.SetValue(default_end)
        history_query_row.Add(self._history_end_picker, 0, wx.ALL, 6)
        self._history_query_button = wx.Button(self, label="查詢 (Query)")
        self._history_query_button.Bind(wx.EVT_BUTTON, self._on_query_history)
        history_query_row.Add(self._history_query_button, 0, wx.ALL, 6)
        sizer.Add(history_query_row, 0)

        self._history_list = wx.ListBox(self, size=(-1, 160))
        sizer.Add(self._history_list, 0, wx.EXPAND | wx.ALL, 6)

        self.SetSizer(sizer)
        self._refresh()

    def refresh(self) -> None:
        """Called by `ReadinessFrame` on every broker/instrument-selection/market-data
        event — see module docstring for why the subscriptions live there."""
        self._refresh()

    def _on_query_history(self, _event: wx.CommandEvent) -> None:
        current = self._services.instrument_selection.current
        if current is None:
            return
        start_date = _wx_date_to_date(self._history_start_picker.GetValue())
        end_date = _wx_date_to_date(self._history_end_picker.GetValue())
        records = self._services.market_data_bar_service.query_history(
            current.instrument, current.contract, start_date=start_date, end_date=end_date
        )
        self._history_list.Set(
            [_format_record(r) for r in reversed(records[-_HISTORY_ROWS_SHOWN:])]
        )

    def _refresh(self) -> None:
        current = self._services.instrument_selection.current
        if current is None:
            self._status_label.SetLabel("（尚未選擇契約）")
            self._forming_label.SetLabel("（尚無資料）")
            self._recent_list.Set([])
            self._history_range_label.SetLabel("（尚未收錄任何資料）")
            self._history_list.Set([])
            return

        service = self._services.market_data_bar_service
        instrument, contract = current.instrument, current.contract

        recorded_range = service.recorded_range(instrument, contract)
        if recorded_range is None:
            self._history_range_label.SetLabel(
                "尚無收錄資料 — 本軟體僅在運行期間自行聚合行情，首次啟用時歷史為空"
            )
        else:
            since_str = recorded_range.earliest_at.value.strftime("%Y-%m-%d %H:%M")
            latest_str = recorded_range.latest_at.value.strftime("%Y-%m-%d %H:%M")
            note = "" if recorded_range.covers_full_window else "（尚未滿兩個月，屬正常狀態）"
            self._history_range_label.SetLabel(
                f"自 {since_str} 起開始收錄，最新至 {latest_str}{note}"
            )

        is_stale = service.is_stale(instrument, contract)
        has_gap = service.has_gap(instrument, contract)
        last_update = service.last_update_at(instrument, contract)
        last_update_str = "無" if last_update is None else last_update.value.strftime("%H:%M:%S")
        status_parts = [
            "STALE" if is_stale else "FRESH",
            f"最後更新 {last_update_str}",
        ]
        if has_gap:
            status_parts.append("GAP — 資料缺口，暫無法確認 K 棒完整性")
        streak_color, streak_length = service.candle_streak(instrument, contract)
        if streak_color is not None:
            status_parts.append(f"連續 {_CANDLE_LABEL_ZH[streak_color]}K x{streak_length}")
        self._status_label.SetLabel("｜".join(status_parts))

        forming = service.forming_bar(instrument, contract)
        self._forming_label.SetLabel("（尚無資料）" if forming is None else _format_bar(forming))

        recent = service.recent_closed_bars(instrument, contract, limit=_RECENT_BARS_SHOWN)
        self._recent_list.Set([_format_bar(bar) for bar in reversed(recent)])
