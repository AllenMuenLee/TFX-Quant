"""`python -m tfx_quant.desktop` — launches the startup diagnostics screen.

Sends no orders. Nothing in this feature can — see StartupSafetyGate and the domain
layer's total absence of an order-sending code path.
"""

from __future__ import annotations

import os
import platform
import sys
import traceback
from pathlib import Path

from tfx_quant import __version__
from tfx_quant.desktop.app import TfxQuantApp
from tfx_quant.desktop.composition import build_services, load_settings, log_startup_readiness
from tfx_quant.domain.strategy_state import StrategyState
from tfx_quant.telemetry import get_logger, log_critical, log_info
from tfx_quant.telemetry.setup import configure_logging

_DEFAULT_SETTINGS_PATH = Path(__file__).parent / "settings.example.json"
_DEFAULT_LOG_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home()))
) / "tfx_quant" / "logs"

_logger = get_logger(__name__)


def main() -> int:
    configure_logging(_DEFAULT_LOG_DIR)
    log_info(
        _logger,
        "application_start",
        app_version=__version__,
        python_version=platform.python_version(),
    )
    settings_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_SETTINGS_PATH
    settings = load_settings(settings_path)
    services = build_services(settings)
    services.event_coordinator.start()
    services.market_data_bar_service.start()
    log_startup_readiness(services)
    try:
        app = TfxQuantApp(services)
        app.MainLoop()
    except Exception as exc:
        _handle_uncaught_exception(services, exc)
        raise
    finally:
        services.market_data_bar_service.stop()
        services.event_coordinator.stop(timeout=5)
    return 0


def _handle_uncaught_exception(services: object, exc: BaseException) -> None:
    """Per the global safety rule: any uncaught exception must be routed toward a
    safe pause, never silently swallowed or left to crash without a trace."""
    from tfx_quant.desktop.composition import ServiceContainer

    assert isinstance(services, ServiceContainer)
    state_machine = services.strategy_state_machine
    # PAUSED_SAFE is only reachable from RUNNING (see `domain/strategy_state.py`'s
    # transition table) — anywhere else, FAULTED is the state machine's actual
    # "stop trading, needs operator attention" terminal for an unexpected failure.
    if state_machine.can_transition(state_machine.state, StrategyState.PAUSED_SAFE):
        resulting_state = StrategyState.PAUSED_SAFE
    elif state_machine.can_transition(state_machine.state, StrategyState.FAULTED):
        resulting_state = StrategyState.FAULTED
    else:
        resulting_state = None
    if resulting_state is not None:
        state_machine.transition(resulting_state)
    log_critical(
        _logger,
        "uncaught_exception",
        exception_type=type(exc).__name__,
        stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        affected_module=type(exc).__module__,
        resulting_strategy_state=resulting_state.value if resulting_state is not None else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
