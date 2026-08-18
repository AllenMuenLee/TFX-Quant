"""The wxPython application shell."""

from __future__ import annotations

import wx

from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.desktop.readiness_frame import ReadinessFrame


class TfxQuantApp(wx.App):
    def __init__(self, services: ServiceContainer) -> None:
        self._services = services
        super().__init__()

    def OnInit(self) -> bool:  # noqa: N802 - wx API name
        frame = ReadinessFrame(None, self._services)
        frame.Show()
        self.SetTopWindow(frame)
        return True
