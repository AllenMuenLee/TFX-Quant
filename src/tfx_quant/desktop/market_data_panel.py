"""Candlestick view for locally recorded Yuanta quote data."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import wx
import wx.adv

from tfx_quant.application.ports.quote_gateway import QuoteConnectionState
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.timestamp import TAIPEI_TZ

_BG = wx.Colour(15, 23, 42)
_CARD = wx.Colour(24, 34, 55)
_GRID = wx.Colour(51, 65, 85)
_TEXT = wx.Colour(203, 213, 225)
_UP = wx.Colour(239, 68, 68)
_DOWN = wx.Colour(34, 197, 94)
_LIVE_LIMIT = 80
_MIN_VISIBLE_BARS = 8


def _wx_date(value: date) -> wx.DateTime:
    result = wx.DateTime()
    result.Set(value.day, value.month - 1, value.year)
    return result


def _date(value: wx.DateTime) -> date:
    return date(value.GetYear(), value.GetMonth() + 1, value.GetDay())


class CandlestickCanvas(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_CARD)
        self.SetMinSize((-1, 390))
        self._bars: list[Bar] = []
        self._zoom = 1.0
        self._right_index = 0
        self._drag_x: int | None = None
        self._empty_message = "等待元大行情 API 資料…"
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_mouse_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)

    def _on_size(self, event: wx.SizeEvent) -> None:
        self.Refresh()
        event.Skip()

    def set_bars(self, bars: list[Bar], empty_message: str = "此區間沒有 K 線") -> None:
        was_at_latest = self._right_index >= len(self._bars)
        self._bars = bars
        self._empty_message = empty_message
        if was_at_latest or not self._right_index:
            self._right_index = len(bars)
        else:
            self._right_index = min(self._right_index, len(bars))
        self.Refresh()

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.25)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.25)

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._right_index = len(self._bars)
        self.Refresh()

    def _set_zoom(self, zoom: float) -> None:
        self._zoom = min(10.0, max(0.25, zoom))
        self.Refresh()

    def _visible_range(self, plot_width: int) -> tuple[int, int]:
        count = len(self._bars)
        if count == 0:
            return 0, 0
        base_visible = max(_MIN_VISIBLE_BARS, plot_width // 10)
        visible = min(count, max(1, max(_MIN_VISIBLE_BARS, round(base_visible / self._zoom))))
        right = min(count, max(visible, self._right_index))
        return right - visible, right

    def _scroll(self, bars: int, plot_width: int | None = None) -> None:
        width = plot_width if plot_width is not None else max(1, self.GetClientSize().width - 86)
        start, end = self._visible_range(width)
        visible = end - start
        self._right_index = min(len(self._bars), max(visible, self._right_index + bars))
        self.Refresh()

    def _on_mouse_wheel(self, event: wx.MouseEvent) -> None:
        direction = 1 if event.GetWheelRotation() > 0 else -1
        if event.ControlDown():
            self._set_zoom(self._zoom * (1.2 if direction > 0 else 1 / 1.2))
        else:
            width = max(1, self.GetClientSize().width - 86)
            start, end = self._visible_range(width)
            step = max(1, (end - start) // 8)
            self._scroll(-direction * step, width)

    def _on_left_down(self, event: wx.MouseEvent) -> None:
        self._drag_x = event.GetX()
        self.CaptureMouse()

    def _on_left_up(self, _event: wx.MouseEvent) -> None:
        self._drag_x = None
        if self.HasCapture():
            self.ReleaseMouse()

    def _on_capture_lost(self, _event: wx.MouseCaptureLostEvent) -> None:
        self._drag_x = None

    def _on_motion(self, event: wx.MouseEvent) -> None:
        if self._drag_x is None or not event.Dragging() or not event.LeftIsDown():
            return
        width = max(1, self.GetClientSize().width - 86)
        start, end = self._visible_range(width)
        pixels_per_bar = width / max(1, end - start)
        delta = event.GetX() - self._drag_x
        bars = round(-delta / max(1.0, pixels_per_bar))
        if bars:
            self._scroll(bars, width)
            self._drag_x = event.GetX()

    def _paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(_CARD))
        dc.Clear()
        width, height = self.GetClientSize()
        if not self._bars or width < 120 or height < 100:
            dc.SetTextForeground(_TEXT)
            dc.DrawLabel(self._empty_message, wx.Rect(0, 0, width, height), wx.ALIGN_CENTER)
            return

        left, top, right, bottom = 68, 22, 18, 38
        plot_w, plot_h = max(1, width - left - right), max(1, height - top - bottom)
        start_index, end_index = self._visible_range(plot_w)
        visible_bars = self._bars[start_index:end_index]
        low = min(float(bar.low.amount) for bar in visible_bars)
        high = max(float(bar.high.amount) for bar in visible_bars)
        # Use the same zoom factor on both axes. At reset (1.0), the visible
        # extrema touch the plot edges; zooming changes both candle width and height.
        midpoint = (high + low) / 2
        half_range = max((high - low) / 2, 0.5) / self._zoom
        low, high = midpoint - half_range, midpoint + half_range

        dc.SetPen(wx.Pen(_GRID, 1))
        dc.SetTextForeground(wx.Colour(148, 163, 184))
        for i in range(5):
            y = top + round(plot_h * i / 4)
            price = high - (high - low) * i / 4
            dc.DrawLine(left, y, left + plot_w, y)
            dc.DrawText(f"{price:,.0f}", 8, y - 8)

        count = len(visible_bars)
        step = plot_w / max(count, 1)
        body_w = max(2, min(12, int(step * 0.62)))

        def y_of(value: Decimal) -> int:
            return top + round((high - float(value)) / (high - low) * plot_h)

        for index, bar in enumerate(visible_bars):
            x = left + round((index + 0.5) * step)
            color = _UP if bar.close.amount >= bar.open.amount else _DOWN
            dc.SetPen(wx.Pen(color, 1))
            dc.DrawLine(x, y_of(bar.high.amount), x, y_of(bar.low.amount))
            y_open, y_close = y_of(bar.open.amount), y_of(bar.close.amount)
            y_body, body_h = min(y_open, y_close), max(2, abs(y_close - y_open))
            dc.SetBrush(wx.Brush(color))
            dc.DrawRectangle(x - body_w // 2, y_body, body_w, body_h)

        label_count = min(6, count)
        for i in range(label_count):
            index = round(i * (count - 1) / max(label_count - 1, 1))
            bar = visible_bars[index]
            x = left + round((index + 0.5) * step)
            dc.DrawText(bar.start.value.strftime("%m/%d %H:%M"), x - 35, top + plot_h + 10)


class MarketDataPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services
        self._live_mode = True
        self.SetBackgroundColour(_BG)
        self._canvas = CandlestickCanvas(self)

        root = wx.BoxSizer(wx.VERTICAL)
        header = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(self, label="市場行情")
        title.SetForegroundColour(wx.WHITE)
        title.SetFont(title.GetFont().Bold().Scale(1.35))
        header.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        header.AddStretchSpacer()
        self._source = wx.StaticText(self, label="● 元大行情 API · 需行情帳號登入")
        self._source.SetForegroundColour(wx.Colour(96, 165, 250))
        header.Add(self._source, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(header, 0, wx.EXPAND | wx.BOTTOM, 12)

        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        self._live_button = wx.Button(self, label="即時監看")
        self._history_button = wx.Button(self, label="歷史查詢")
        self._live_button.Bind(wx.EVT_BUTTON, lambda _e: self._set_mode(True))
        self._history_button.Bind(wx.EVT_BUTTON, lambda _e: self._set_mode(False))
        mode_row.Add(self._live_button, 0, wx.RIGHT, 8)
        mode_row.Add(self._history_button, 0)
        mode_row.AddStretchSpacer()
        navigation_hint = wx.StaticText(self, label="滾輪平移 · Ctrl+滾輪縮放 · 拖曳平移")
        navigation_hint.SetForegroundColour(_TEXT)
        mode_row.Add(navigation_hint, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        zoom_out = wx.Button(self, label="−", style=wx.BU_EXACTFIT)
        zoom_out.SetToolTip("縮小 K 線")
        zoom_out.Bind(wx.EVT_BUTTON, lambda _e: self._canvas.zoom_out())
        mode_row.Add(zoom_out, 0, wx.RIGHT, 4)
        zoom_in = wx.Button(self, label="+", style=wx.BU_EXACTFIT)
        zoom_in.SetToolTip("放大 K 線")
        zoom_in.Bind(wx.EVT_BUTTON, lambda _e: self._canvas.zoom_in())
        mode_row.Add(zoom_in, 0, wx.RIGHT, 4)
        reset_view = wx.Button(self, label="重設", style=wx.BU_EXACTFIT)
        reset_view.Bind(wx.EVT_BUTTON, lambda _e: self._canvas.reset_view())
        mode_row.Add(reset_view, 0)
        root.Add(mode_row, 0, wx.BOTTOM, 10)

        self._history_controls = wx.Panel(self)
        self._history_controls.SetBackgroundColour(_BG)
        range_row = wx.BoxSizer(wx.HORIZONTAL)
        self._start = wx.adv.DatePickerCtrl(self._history_controls)
        self._start.SetValue(_wx_date(date.today() - timedelta(days=7)))
        self._end = wx.adv.DatePickerCtrl(self._history_controls)
        self._end.SetValue(_wx_date(date.today()))
        query = wx.Button(self._history_controls, label="查詢")
        query.Bind(wx.EVT_BUTTON, lambda _e: self.refresh())
        range_row.Add(
            wx.StaticText(self._history_controls, label="日期"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        range_row.Add(self._start, 0, wx.RIGHT, 8)
        range_row.Add(
            wx.StaticText(self._history_controls, label="至"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        range_row.Add(self._end, 0, wx.RIGHT, 8)
        range_row.Add(query, 0)
        self._history_controls.SetSizer(range_row)
        root.Add(self._history_controls, 0, wx.BOTTOM, 10)

        self._status = wx.StaticText(self, label="尚未連接元大行情 API")
        self._status.SetForegroundColour(_TEXT)
        root.Add(self._status, 0, wx.BOTTOM, 8)
        root.Add(self._canvas, 1, wx.EXPAND)
        self.SetSizer(root)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self.refresh(), self._timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._timer.Start(5000)
        self._set_mode(True)

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() is self:
            self._timer.Stop()
        event.Skip()

    def _set_mode(self, live: bool) -> None:
        self._live_mode = live
        self._history_controls.Show(not live)
        self._live_button.Enable(not live)
        self._history_button.Enable(live)
        self.Layout()
        self.refresh()

    def refresh(self) -> None:
        self._services.quote_runtime.refresh()
        current = self._services.instrument_selection.current
        if current is None:
            self._status.SetLabel("請選擇要監看的商品")
            self._canvas.set_bars([], "尚未選擇商品")
            return
        service = self._services.quote_runtime
        instrument, contract = current.instrument, current.contract
        if self._live_mode:
            end = date.today()
            records = service.query(end - timedelta(days=10), end)
            bars = [record.bar for record in records[-_LIVE_LIMIT:]]
            forming = service.forming_bar
            if forming is not None and (not bars or bars[-1].start != forming.start):
                bars.append(forming)
            last_event = service.last_event_at
            if last_event is None:
                updated = "尚未收到行情事件"
            else:
                updated = (
                    f"最後接收 {last_event.value.astimezone(TAIPEI_TZ):%H:%M:%S}"
                    f"（{service.event_count} 筆）"
                )
            health = {
                QuoteConnectionState.LOGGED_ON: "即時行情已登入",
                QuoteConnectionState.CONNECTING: "行情連線中",
                QuoteConnectionState.CONNECTED: "行情已連線，等待登入",
                QuoteConnectionState.STALE: "行情連線中斷",
                QuoteConnectionState.FAILED: "行情登入失敗",
                QuoteConnectionState.IDLE: "行情尚未連線",
                QuoteConnectionState.STOPPED: "行情尚未登入或目前休市",
            }.get(service.state, f"行情狀態：{service.state.value}")
            self._status.SetLabel(
                f"{instrument.display_name_zh} · 自動近月 {contract.code}  |  {health} · {updated}"
            )
        else:
            start, end = _date(self._start.GetValue()), _date(self._end.GetValue())
            records = service.query(start, end)
            bars = [record.bar for record in records]
            summary = f"{start} — {end} · {len(bars)} 根"
            self._status.SetLabel(f"{instrument.display_name_zh} · {contract.code}  |  {summary}")
        self._canvas.set_bars(bars)
