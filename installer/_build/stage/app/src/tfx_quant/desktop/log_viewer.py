"""Human-readable process log window."""

from __future__ import annotations

import wx

from tfx_quant.telemetry.setup import get_log_lines


class LogViewerFrame(wx.Frame):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, title="所有日誌", size=(1000, 650))
        self._text = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL | wx.TE_DONTWRAP,
        )
        self._text.SetFont(wx.Font(wx.FontInfo(10).Family(wx.FONTFAMILY_TELETYPE)))
        self._last_lines: tuple[str, ...] = ()
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._timer.Start(500)
        self._refresh()

    def _on_timer(self, _event: wx.TimerEvent) -> None:
        self._refresh()

    def _refresh(self) -> None:
        lines = get_log_lines()
        if lines == self._last_lines:
            return
        self._last_lines = lines
        self._text.SetValue("\n".join(lines))
        self._text.ShowPosition(self._text.GetLastPosition())

    def _on_close(self, event: wx.CloseEvent) -> None:
        self._timer.Stop()
        event.Skip()
