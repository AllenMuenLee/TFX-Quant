from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pydantic import SecretStr

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.settings.trading_settings import Environment, TradingSettings
from tfx_quant.desktop.composition import RuntimeOverrides, ServiceContainer, build_services
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.simulation.clock import VirtualClock
from tfx_quant.simulation.identity import DeterministicIdGenerator
from tfx_quant.simulation.market_data_control import SimulationMarketDataController
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)


def _simulation_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    path = root / "tfx_quant" / "simulation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_simulation_services(settings: TradingSettings, *, seed: int = 15001) -> ServiceContainer:
    """Build the desktop with offline adapters and visibly isolated persistence."""
    data_dir = _simulation_data_dir()
    isolated = settings.model_copy(
        update={
            "environment": Environment.TEST,
            "market_data_db_path": str(data_dir / "market_data.sqlite3"),
            "order_db_path": str(data_dir / "orders.sqlite3"),
            "reversal_workflow_db_path": str(data_dir / "reversal_workflows.sqlite3"),
            "position_baseline_db_path": str(data_dir / "position_baselines.sqlite3"),
            "eod_flatten_workflow_db_path": str(data_dir / "eod_flatten_workflows.sqlite3"),
        }
    )
    # A documented trading-session instant, independent of the host wall clock.
    clock = VirtualClock(Timestamp(datetime(2026, 8, 25, 10, 45, tzinfo=TAIPEI_TZ)))

    def broker_factory(bus: EventCoordinator) -> tuple[MockTradeGateway, MockBrokerSession]:
        return (
            MockTradeGateway(logged_in=True, event_publisher=bus),
            MockBrokerSession(event_publisher=bus),
        )

    market_data = SimulationMarketDataController()
    services = build_services(
        isolated,
        runtime_overrides=RuntimeOverrides(
            clock=clock,
            id_generator=DeterministicIdGenerator(seed),
            broker_factory=broker_factory,
            quote_gateway_factory=market_data.gateway_factory,
            simulation=True,
            simulation_market_data=market_data,
        ),
    )
    market_data.attach(services.quote_runtime)
    log_info(
        _logger,
        "simulation_runtime_built",
        simulation=True,
        scenario_id="desktop-mock",
        fixture_version="none",
        random_seed=seed,
        virtual_clock=clock.now().value.isoformat(),
        speed=1.0,
        persistence_directory=str(data_dir),
    )
    return services


def start_simulation_sessions(services: ServiceContainer) -> None:
    """Start offline trading and the default no-login mock quote source."""
    if not services.simulation:
        raise RuntimeError("refusing to start simulation sessions in a real runtime")
    request = LoginRequest(
        environment=Environment.TEST,
        user_id="SIMULATION",
        password=SecretStr("SIMULATION-ONLY"),
    )
    services.broker_session.start(request)
    if services.simulation_market_data is None:
        raise RuntimeError("simulation market-data control is missing")
    services.simulation_market_data.use_mock_data()
    log_info(_logger, "simulation_sessions_started", simulation=True)
