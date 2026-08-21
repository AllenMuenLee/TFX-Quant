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
        # Feature 08's "回到前景時查詢持倉與活動委託" trigger — fires whenever the app
        # window becomes the active/foreground window (never on losing focus).
        self.Bind(wx.EVT_ACTIVATE_APP, self._on_activate_app)
        return True

    def _on_activate_app(self, event: wx.ActivateEvent) -> None:
        if event.GetActive():
            self._services.reconciliation_service.on_foreground_return()
        event.Skip()
