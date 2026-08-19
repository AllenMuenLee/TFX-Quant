"""The composition root — the one place concrete services get wired together.

Chooses mock vs. real Yuanta gateways off `TradingSettings.use_mock`. Every service
here is constructed behind an application-layer Protocol (`Clock`, `IdGenerator`,
`TradeGatewayPort`, `QuoteGatewayPort`), so tests can substitute their own
implementations without touching this module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.instrument_selection.instrument_selection_service import (
    InstrumentSelectionService,
)
from tfx_quant.application.market_data.bar_service import MarketDataBarService
from tfx_quant.application.ports.bar_signal_state import BarSignalStateStore
from tfx_quant.application.ports.broker_session import IBrokerSession
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.identity import IdGenerator
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.application.ports.yuanta_gateways import QuoteGatewayPort, TradeGatewayPort
from tfx_quant.application.settings.trading_settings import TradingSettings, validate_startup
from tfx_quant.domain.strategy_state import StrategyStateMachine
from tfx_quant.infrastructure.clock import SystemClock
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.market_data.trading_calendar_repository import (
    JsonTradingCalendarRepository,
)
from tfx_quant.infrastructure.yuanta import login_preferences
from tfx_quant.infrastructure.yuanta.broker_session_gateway_views import (
    BrokerSessionQuoteGatewayView,
    BrokerSessionTradeGatewayView,
)
from tfx_quant.infrastructure.yuanta.instrument_master_repository import (
    JsonInstrumentMasterRepository,
)
from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
from tfx_quant.infrastructure.yuanta.mock_quote_gateway import MockQuoteGateway
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.infrastructure.yuanta.preflight import raise_if_any_failed, run_preflight_checks
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator
from tfx_quant.persistence.sqlite_bar_record_repository import SqliteBarRecordRepository
from tfx_quant.persistence.sqlite_connection import create_connection

_DEFAULT_INSTRUMENT_MASTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "yuanta"
    / "instrument_master.example.json"
)
_DEFAULT_TRADING_CALENDAR_PATH = (
    Path(__file__).resolve().parents[1]
    / "infrastructure"
    / "market_data"
    / "trading_calendar.example.json"
)


@dataclass
class ServiceContainer:
    settings: TradingSettings
    clock: Clock
    id_generator: IdGenerator
    trade_gateway: TradeGatewayPort
    quote_gateway: QuoteGatewayPort
    broker_session: IBrokerSession
    event_coordinator: EventCoordinator
    strategy_state_machine: StrategyStateMachine
    instrument_master: InstrumentMasterRepository
    instrument_selection: InstrumentSelectionService
    market_data_bar_service: MarketDataBarService


def load_settings(path: Path) -> TradingSettings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validate_startup(raw)


def _resolve_instrument_master_path(settings: TradingSettings) -> Path:
    if settings.instrument_master_path is None:
        return _DEFAULT_INSTRUMENT_MASTER_PATH
    return Path(settings.instrument_master_path)


def _resolve_trading_calendar_path(settings: TradingSettings) -> Path:
    if settings.trading_calendar_path is None:
        return _DEFAULT_TRADING_CALENDAR_PATH
    return Path(settings.trading_calendar_path)


def _resolve_market_data_db_path(settings: TradingSettings) -> Path:
    """Unlike the instrument master/trading calendar paths, there is no bundled example
    to fall back to — this is a per-user, per-machine data file that starts empty (see
    `TradingSettings.market_data_db_path`'s docstring) and accumulates only from bars
    this software has actually recorded itself."""
    if settings.market_data_db_path is not None:
        return Path(settings.market_data_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "market_data.sqlite3"


def build_services(settings: TradingSettings) -> ServiceContainer:
    clock: Clock = SystemClock()
    id_generator: IdGenerator = UuidIdGenerator()
    event_coordinator = EventCoordinator()
    strategy_state_machine = StrategyStateMachine()
    instrument_master: InstrumentMasterRepository = JsonInstrumentMasterRepository(
        _resolve_instrument_master_path(settings)
    )
    trading_calendar: TradingCalendarRepository = JsonTradingCalendarRepository(
        _resolve_trading_calendar_path(settings)
    )
    market_data_connection = create_connection(
        _resolve_market_data_db_path(settings), check_same_thread=False
    )
    bar_record_repository = SqliteBarRecordRepository(market_data_connection)
    market_data_bar_service = MarketDataBarService(
        event_bus=event_coordinator,
        clock=clock,
        trading_calendar_repository=trading_calendar,
        instrument_master=instrument_master,
        bar_record_repository=bar_record_repository,
    )
    bar_signal_state_store: BarSignalStateStore = market_data_bar_service

    trade_gateway: TradeGatewayPort
    quote_gateway: QuoteGatewayPort
    broker_session: IBrokerSession
    if settings.use_mock:
        trade_gateway = MockTradeGateway()
        quote_gateway = MockQuoteGateway()
        broker_session = MockBrokerSession(event_publisher=event_coordinator)
    else:
        raise_if_any_failed(run_preflight_checks())

        # Imported lazily: this module pulls in pythonnet/CLR-hosting code that only
        # needs to exist for the real (non-mock) branch — see
        # docs/adr/0004-broker-session-architecture.md for why this glue is kept as
        # thin and isolated as possible.
        from tfx_quant.infrastructure.yuanta.spark_api_adapter import SparkApiSessionAdapter

        adapter = SparkApiSessionAdapter()
        orchestrator = BrokerSessionOrchestrator(
            adapter=adapter,
            event_coordinator=event_coordinator,
            # Environment is chosen per login attempt by the operator (see
            # `desktop/login_dialog.py`), not fixed at composition time — see
            # `application/ports/broker_session.LoginRequest`.
            account_no_hint=login_preferences.load().remembered_account_no,
        )
        adapter.bind_orchestrator(orchestrator)
        trade_gateway = BrokerSessionTradeGatewayView(orchestrator)
        quote_gateway = BrokerSessionQuoteGatewayView(orchestrator, instrument_master)
        broker_session = orchestrator

    instrument_selection = InstrumentSelectionService(
        strategy_state_machine=strategy_state_machine,
        trade_gateway=trade_gateway,
        quote_gateway=quote_gateway,
        instrument_master=instrument_master,
        bar_signal_state_store=bar_signal_state_store,
        clock=clock,
        event_publisher=event_coordinator,
    )

    return ServiceContainer(
        settings=settings,
        clock=clock,
        id_generator=id_generator,
        trade_gateway=trade_gateway,
        quote_gateway=quote_gateway,
        broker_session=broker_session,
        event_coordinator=event_coordinator,
        strategy_state_machine=strategy_state_machine,
        instrument_master=instrument_master,
        instrument_selection=instrument_selection,
        market_data_bar_service=market_data_bar_service,
    )


def compute_readiness(services: ServiceContainer) -> list[tuple[str, bool]]:
    """Per-module readiness for the startup diagnostics screen.

    Never includes credentials or raw account numbers — only booleans/labels. The five
    `SessionCapabilities` rows are listed independently on purpose: the implementation
    prompt explicitly forbids collapsing "logged in" into "can trade" — see
    `application/ports/broker_session.py`.
    """
    capabilities = services.broker_session.capabilities
    return [
        ("Settings loaded and validated", True),
        ("Event coordinator running", services.event_coordinator.is_running),
        ("Broker session: login", capabilities.login),
        ("Broker session: market data", capabilities.market_data),
        ("Broker session: trading", capabilities.trading),
        ("Broker session: order reports", capabilities.order_reports),
        ("Broker session: queries", capabilities.queries),
        (
            "Market data: bar history persistence",
            not services.market_data_bar_service.is_persistence_degraded(),
        ),
    ]
