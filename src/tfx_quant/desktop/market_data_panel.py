"""MarketDataPanel — Feature 04's forming/closed-bar and staleness display.

Embedded in `ReadinessFrame`, mirroring `InstrumentSelectionPanel`'s embedding: purely a
display surface (no order-sending control, matching this codebase's existing UI
constraint) with a `refresh()` the parent frame calls. `ReadinessFrame` — not this panel
— owns the market-data event subscriptions (`BarClosed`/`MarketDataTickReceived`/
`MarketDataFreshnessChanged`/`MarketDataGapDetected`/`MarketDataGapCleared`) and their
`wx.CallAfter` hop off the `EventCoordinator` consumer thread, same as it already does
for broker session events — keeping subscription lifecycle in one place (the frame's
existing `_on_close` teardown) rather than duplicating it per embedded panel.

The closed-bar list is a single view backed by `MarketDataBarService.query_history()`
(persisted history, up to the rolling two-month window) rather than two separate
"recent" vs "historical" lists — a bar that closed moments ago and a bar from three
weeks ago are the same kind of record once persisted, so they share one list and one
date-range query. The date pickers/查詢 button are for quickly jumping to a different
day; every `_refresh()` (i.e. every market-data event) re-runs the *current* date range
so today's view keeps updating live without the user needing to press 查詢 again.
"""

from __future__ import annotations

from datetime import date, timedelta

import wx
import wx.adv

from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.bar_record import BarRecord
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)

_CANDLE_LABEL_ZH = {
    CandleColor.RED: "紅",
    CandleColor.BLACK: "黑",
    CandleColor.DOJI: "十字",
}
_BAR_ROWS_SHOWN = 200
_DEFAULT_QUERY_DAYS = 7


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

        self._range_label = wx.StaticText(self, label="（尚未收錄任何資料）")
        sizer.Add(self._range_label, 0, wx.ALL, 6)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        nav_row = wx.BoxSizer(wx.HORIZONTAL)
        nav_row.Add(
            wx.StaticText(self, label="收盤 K 棒（本機自行收錄）　查詢日期 起："),
            0,
            wx.ALL | wx.ALIGN_CENTER_VERTICAL,
            6,
        )
        default_end = wx.DateTime.Now()
        default_start_date = date.today() - timedelta(days=_DEFAULT_QUERY_DAYS)
        default_start = _date_to_wx_date(default_start_date)
        self._start_picker = wx.adv.DatePickerCtrl(self)
        self._start_picker.SetValue(default_start)
        nav_row.Add(self._start_picker, 0, wx.ALL, 6)
        nav_row.Add(wx.StaticText(self, label="迄："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        self._end_picker = wx.adv.DatePickerCtrl(self)
        self._end_picker.SetValue(default_end)
        nav_row.Add(self._end_picker, 0, wx.ALL, 6)
        self._query_button = wx.Button(self, label="查詢 (Query)")
        self._query_button.Bind(wx.EVT_BUTTON, self._on_query)
        nav_row.Add(self._query_button, 0, wx.ALL, 6)
        sizer.Add(nav_row, 0)

        self._bar_list = wx.ListBox(self, size=(-1, 220))
        sizer.Add(self._bar_list, 0, wx.EXPAND | wx.ALL, 6)

        self.SetSizer(sizer)
        self._refresh()

    def refresh(self) -> None:
        """Called by `ReadinessFrame` on every broker/instrument-selection/market-data
        event — see module docstring for why the subscriptions live there."""
        self._refresh()

    def _on_query(self, _event: wx.CommandEvent) -> None:
        start_date, end_date = self._selected_date_range()
        log_info(
            _logger,
            "bar_history_browse_query_requested",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )
        self._refresh()

    def _selected_date_range(self) -> tuple[date, date]:
        return (
            _wx_date_to_date(self._start_picker.GetValue()),
            _wx_date_to_date(self._end_picker.GetValue()),
        )

    def _refresh(self) -> None:
        current = self._services.instrument_selection.current
        if current is None:
            self._status_label.SetLabel("（尚未選擇契約）")
            self._forming_label.SetLabel("（尚無資料）")
            self._range_label.SetLabel("（尚未收錄任何資料）")
            self._bar_list.Set([])
            return

        service = self._services.market_data_bar_service
        instrument, contract = current.instrument, current.contract

        recorded_range = service.recorded_range(instrument, contract)
        if recorded_range is None:
            self._range_label.SetLabel(
                "尚無收錄資料 — 本軟體僅在運行期間自行聚合行情，首次啟用時歷史為空"
            )
        else:
            since_str = recorded_range.earliest_at.value.strftime("%Y-%m-%d %H:%M")
            latest_str = recorded_range.latest_at.value.strftime("%Y-%m-%d %H:%M")
            note = "" if recorded_range.covers_full_window else "（尚未滿兩個月，屬正常狀態）"
            self._range_label.SetLabel(f"自 {since_str} 起開始收錄，最新至 {latest_str}{note}")

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

        start_date, end_date = self._selected_date_range()
        records = service.query_history(
            instrument, contract, start_date=start_date, end_date=end_date
        )
        self._bar_list.Set([_format_record(r) for r in reversed(records[-_BAR_ROWS_SHOWN:])])
