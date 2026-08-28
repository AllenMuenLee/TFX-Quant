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
from tfx_quant.desktop.app import TfxQuantApp
from tfx_quant.desktop.composition import (
    auto_select_startup_instrument,
    build_services,
    load_settings,
    log_startup_readiness,
)
from tfx_quant.domain.strategy_state import attempt_safe_pause
from tfx_quant.telemetry import get_logger, log_critical, log_info
from tfx_quant.telemetry.audit import install_audit_handler
from tfx_quant.telemetry.setup import configure_logging

_DEFAULT_SETTINGS_PATH = Path(__file__).parent / "settings.example.json"
_DEFAULT_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "tfx_quant" / "logs"

_logger = get_logger(__name__)


def _parse_args(argv: list[str]) -> tuple[Path, bool]:
    parser = ArgumentParser(prog="tfx-quant-desktop")
    parser.add_argument("settings", nargs="?", type=Path, default=_DEFAULT_SETTINGS_PATH)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="run the isolated offline simulator; never load Yuanta OCX adapters",
    )
    args = parser.parse_args(argv)
    return args.settings, bool(args.mock)


def main(argv: list[str] | None = None) -> int:
    settings_path, mock = _parse_args(sys.argv[1:] if argv is None else argv)
    configure_logging(_DEFAULT_LOG_DIR)
    log_info(
        _logger,
        "application_start",
        app_version=__version__,
        python_version=platform.python_version(),
        simulation=mock,
    )
    settings = load_settings(settings_path)
    if mock:
        # Kept out of normal startup's import graph: the simulator is reachable only
        # through this explicit CLI switch.
        from tfx_quant.simulation.composition import build_simulation_services

        services = build_simulation_services(settings)
    else:
        services = build_services(settings)
    install_audit_handler(
        _DEFAULT_LOG_DIR / "audit.sqlite3",
        on_critical_failure=lambda exc: _handle_audit_failure(services, exc),
    )
    services.event_coordinator.start()
    auto_select_startup_instrument(services)
    if mock:
        from tfx_quant.simulation.composition import start_simulation_sessions

        start_simulation_sessions(services)
    services.order_manager.start()
    services.reconciliation_service.start()
    services.connectivity_monitor.start()
    services.risk_supervisor.start()
    services.signal_engine_service.start()
    log_startup_readiness(services)
    try:
        app = TfxQuantApp(services)
        app.MainLoop()
    except Exception as exc:
        _handle_uncaught_exception(services, exc)
        raise
    finally:
        services.signal_engine_service.stop()
        services.risk_supervisor.stop()
        services.connectivity_monitor.stop()
        services.reconciliation_service.stop()
        services.order_manager.stop()
        services.quote_runtime.stop()
        services.event_coordinator.stop(timeout=5)
    return 0


def _handle_uncaught_exception(services: object, exc: BaseException) -> None:
    """Per the global safety rule: any uncaught exception must be routed toward a
    safe pause, never silently swallowed or left to crash without a trace."""
    from tfx_quant.desktop.composition import ServiceContainer

    assert isinstance(services, ServiceContainer)
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


def _handle_audit_failure(services: object, exc: Exception) -> None:
    """A critical trading event that cannot be persisted stops automated trading."""
    from tfx_quant.desktop.composition import ServiceContainer

    assert isinstance(services, ServiceContainer)
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
