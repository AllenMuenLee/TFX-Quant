"""The wxPython application shell.

Owns the `ServiceContainer` lifecycle: `OnInit` builds it for the settings-file
environment; `switch_environment` tears the whole container down and rebuilds it for the
other environment when the operator flips the 執行環境 selector (模擬下單 ↔ 正式下單) on
the readiness screen. A rebuild is used rather than hot-swapping the trade adapter
because the two environments must also be visually distinct (per Feature 15) and the real
`LegacyBroker` needs its OCX preflight, so a coherent single-environment build is safer.
"""

from __future__ import annotations

import wx

from tfx_quant.application.settings.trading_settings import Environment, TradingSettings
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.desktop.readiness_frame import ReadinessFrame
from tfx_quant.telemetry import get_logger, log_info
from tfx_quant.telemetry.audit import install_audit_handler

_logger = get_logger(__name__)


class TfxQuantApp(wx.App):
    def __init__(self, settings: TradingSettings) -> None:
        self._settings = settings
        self._services: ServiceContainer | None = None
        self._frame: ReadinessFrame | None = None
        super().__init__()

    @property
    def services(self) -> ServiceContainer | None:
        return self._services

    def OnInit(self) -> bool:  # noqa: N802 - wx API name
        from tfx_quant.desktop.__main__ import (
            AUDIT_DB_PATH,
            build_and_start_services,
            handle_audit_failure,
        )

        self._services = build_and_start_services(self._settings)
        install_audit_handler(
            AUDIT_DB_PATH,
            on_critical_failure=lambda exc: handle_audit_failure(self._services, exc),
        )
        self._open_frame(open_login=False)
        # Feature 08's "回到前景時查詢持倉與活動委託" trigger.
        self.Bind(wx.EVT_ACTIVATE_APP, self._on_activate_app)
        return True

    def OnExit(self) -> int:  # noqa: N802 - wx API name
        from tfx_quant.desktop.__main__ import stop_services

        if self._services is not None:
            stop_services(self._services)
        return 0

    # -- environment switching ---------------------------------------------------------

    def can_switch_environment(self) -> str | None:
        """`None` when it is safe to rebuild for the other environment, otherwise a
        human-readable reason it is refused (strategy live, or orders open)."""
        from tfx_quant.desktop.composition import environment_switch_blocked_reason

        if self._services is None:
            return None
        return environment_switch_blocked_reason(self._services)

    def switch_environment(self, environment: Environment) -> None:
        from tfx_quant.desktop.__main__ import build_and_start_services, stop_services

        if self._services is None or environment is self._settings.environment:
            return
        blocked = self.can_switch_environment()
        if blocked is not None:
            wx.MessageBox(blocked, "無法切換執行環境", wx.OK | wx.ICON_WARNING)
            self._refresh_frame_environment_selector()
            return
        log_info(
            _logger,
            "environment_switch_requested",
            from_environment=self._settings.environment.value,
            to_environment=environment.value,
        )
        stop_services(self._services)
        self._settings = self._settings.model_copy(update={"environment": environment})
        self._services = build_and_start_services(self._settings)
        self._open_frame(open_login=True)

    # -- frame lifecycle -------------------------------------------------------------

    def _open_frame(self, *, open_login: bool) -> None:
        assert self._services is not None
        previous = self._frame
        self._frame = ReadinessFrame(None, self._services)
        self._frame.Show()
        self.SetTopWindow(self._frame)
        if previous is not None:
            previous.Destroy()
        if open_login:
            wx.CallAfter(self._frame.open_login_dialog)

    def _refresh_frame_environment_selector(self) -> None:
        if self._frame is not None:
            self._frame.sync_environment_selector(self._settings.environment)

    def _on_activate_app(self, event: wx.ActivateEvent) -> None:
        if event.GetActive() and self._services is not None:
            self._services.reconciliation_service.on_foreground_return()
        event.Skip()
