"""`python -m tfx_quant.desktop` — launches the startup diagnostics screen.

Sends no orders. Nothing in this feature can — see StartupSafetyGate and the domain
layer's total absence of an order-sending code path.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tfx_quant.desktop.app import TfxQuantApp
from tfx_quant.desktop.composition import build_services, load_settings

_DEFAULT_SETTINGS_PATH = Path(__file__).parent / "settings.example.json"


def main() -> int:
    settings_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_SETTINGS_PATH
    settings = load_settings(settings_path)
    services = build_services(settings)
    services.event_coordinator.start()
    services.market_data_bar_service.start()
    try:
        app = TfxQuantApp(services)
        app.MainLoop()
    finally:
        services.market_data_bar_service.stop()
        services.event_coordinator.stop(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
