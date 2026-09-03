"""`python -m tfx_quant.desktop` — launches the startup diagnostics screen.

`OrderManager` (Feature 06) is wired up and running; `StrategySignalEngineService`
(Feature 05) is its only automatic driver — see `application.strategy_signal.
signal_engine_service.StrategySignalEngineService`. There is still no UI order-entry
control for a human to submit a manual order directly.
"""

from __future__ import annotations

import os
import platform
import sys
import traceback
from argparse import ArgumentParser
from pathlib import Path

from tfx_quant import __version__
from tfx_quant.application.settings.trading_settings import Environment, TradingSettings
from tfx_quant.desktop.app import TfxQuantApp
from tfx_quant.desktop.composition import (
    ServiceContainer,
    auto_select_startup_instrument,
    build_services,
    load_settings,
    log_startup_readiness,
    start_test_env_broker_session,
)
from tfx_quant.domain.strategy_state import attempt_safe_pause
from tfx_quant.telemetry import get_logger, log_critical, log_info
from tfx_quant.telemetry.setup import configure_logging

_DEFAULT_SETTINGS_PATH = Path(__file__).parent / "settings.example.json"
_DEFAULT_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "tfx_quant" / "logs"
AUDIT_DB_PATH = _DEFAULT_LOG_DIR / "audit.sqlite3"

_logger = get_logger(__name__)


def _parse_args(argv: list[str]) -> Path:
    parser = ArgumentParser(prog="tfx-quant-desktop")
    parser.add_argument("settings", nargs="?", type=Path, default=_DEFAULT_SETTINGS_PATH)
    return parser.parse_args(argv).settings


def build_and_start_services(settings: TradingSettings) -> ServiceContainer:
    """Build the whole container for `settings.environment` and start every runtime.

    `TfxQuantApp` calls this again with a different environment when the operator flips
    the 執行環境 selector (模擬下單 ↔ 正式下單) on the readiness screen.
    """
    is_test_env = settings.environment is Environment.TEST
    log_info(
        _logger,
        "application_start",
        app_version=__version__,
        python_version=platform.python_version(),
        environment=settings.environment.value,
        simulation=is_test_env,
    )
    services = build_services(settings)
    services.event_coordinator.start()
    auto_select_startup_instrument(services)
    if is_test_env:
        # 測試環境: start the local broker simulator now; the real quote login is entered
        # by the operator from the readiness screen (quote-only dialog).
        start_test_env_broker_session(services)
    services.order_manager.start()
    services.reconciliation_service.start()
    services.connectivity_monitor.start()
    services.risk_supervisor.start()
    services.signal_engine_service.start()
    log_startup_readiness(services)
    return services


def stop_services(services: ServiceContainer) -> None:
    services.signal_engine_service.stop()
    services.risk_supervisor.stop()
    services.connectivity_monitor.stop()
    services.reconciliation_service.stop()
    services.order_manager.stop()
    services.quote_runtime.stop()
    services.event_coordinator.stop(timeout=5)


def main(argv: list[str] | None = None) -> int:
    settings_path = _parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging(_DEFAULT_LOG_DIR)
    settings = load_settings(settings_path)

    app = TfxQuantApp(settings)
    try:
        app.MainLoop()
    except Exception as exc:
        _handle_uncaught_exception(app.services, exc)
        raise
    return 0


def _handle_uncaught_exception(services: object, exc: BaseException) -> None:
    """Per the global safety rule: any uncaught exception must be routed toward a
    safe pause, never silently swallowed or left to crash without a trace."""
    if not isinstance(services, ServiceContainer):
        return
    # PAUSED_SAFE is only reachable from RUNNING (see `domain/strategy_state.py`'s
    # transition table) — anywhere else, FAULTED is the state machine's actual
    # "stop trading, needs operator attention" terminal for an unexpected failure.
    resulting_state = attempt_safe_pause(services.strategy_state_machine)
    log_critical(
        _logger,
        "uncaught_exception",
        exception_type=type(exc).__name__,
        stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        affected_module=type(exc).__module__,
        resulting_strategy_state=resulting_state.value if resulting_state is not None else None,
    )


def handle_audit_failure(services: object, exc: Exception) -> None:
    """A critical trading event that cannot be persisted stops automated trading."""
    if not isinstance(services, ServiceContainer):
        return
    resulting_state = attempt_safe_pause(services.strategy_state_machine)
    log_critical(
        _logger,
        "critical_audit_persistence_failed_safe_pause",
        exception_type=type(exc).__name__,
        resulting_strategy_state=(
            resulting_state.value
            if resulting_state is not None
            else services.strategy_state_machine.state.value
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
