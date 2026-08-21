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
from tfx_quant.application.market_data.bar_history_backfill_service import (
    BarHistoryBackfillService,
)
from tfx_quant.application.market_data.bar_service import MarketDataBarService
from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.ports.bar_signal_state import BarSignalStateStore
from tfx_quant.application.ports.broker_session import IBrokerSession
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.historical_price_query import HistoricalPriceQueryPort
from tfx_quant.application.ports.identity import IdGenerator
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.application.ports.yuanta_gateways import QuoteGatewayPort, TradeGatewayPort
from tfx_quant.application.position_reconciliation.reconciliation_service import (
    PositionReconciliationService,
)
from tfx_quant.application.reversal_scaling.reversal_service import ReversalWorkflowService
from tfx_quant.application.reversal_scaling.scaling_service import ScalingService
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
from tfx_quant.infrastructure.yuanta.mock_historical_price_query import MockHistoricalPriceQuery
from tfx_quant.infrastructure.yuanta.mock_quote_gateway import MockQuoteGateway
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.infrastructure.yuanta.preflight import raise_if_any_failed, run_preflight_checks
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator
from tfx_quant.persistence.sqlite_bar_record_repository import SqliteBarRecordRepository
from tfx_quant.persistence.sqlite_connection import create_connection
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository
from tfx_quant.persistence.sqlite_position_baseline_repository import (
    SqlitePositionBaselineRepository,
)
from tfx_quant.persistence.sqlite_reversal_workflow_repository import (
    SqliteReversalWorkflowRepository,
)
from tfx_quant.telemetry import get_logger, log_error, log_info

_logger = get_logger(__name__)

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
    bar_history_backfill_service: BarHistoryBackfillService
    order_manager: OrderManager
    reversal_workflow_service: ReversalWorkflowService
    scaling_service: ScalingService
    reconciliation_service: PositionReconciliationService


def load_settings(path: Path) -> TradingSettings:
    log_info(_logger, "settings_load_requested", settings_path=str(path))
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        settings = validate_startup(raw)
    except Exception as exc:
        log_error(
            _logger,
            "settings_validation_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    log_info(
        _logger,
        "settings_validated",
        environment=settings.environment.value,
        selected_instrument=settings.selected_instrument.value,
        contract_selection_mode=settings.contract_selection_mode.value,
        use_mock=settings.use_mock,
        max_net_lots=settings.max_net_lots,
        instrument_master_path_configured=settings.instrument_master_path is not None,
        trading_calendar_path_configured=settings.trading_calendar_path is not None,
        market_data_db_path_configured=settings.market_data_db_path is not None,
        order_db_path_configured=settings.order_db_path is not None,
        reversal_workflow_db_path_configured=settings.reversal_workflow_db_path is not None,
        position_baseline_db_path_configured=settings.position_baseline_db_path is not None,
    )
    return settings


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


def _resolve_order_db_path(settings: TradingSettings) -> Path:
    """Mirrors `_resolve_market_data_db_path` — a separate per-user data file, never the
    same connection (see `docs/adr/0008-order-and-fill-state-machine.md`)."""
    if settings.order_db_path is not None:
        return Path(settings.order_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "orders.sqlite3"


def _resolve_reversal_workflow_db_path(settings: TradingSettings) -> Path:
    """Mirrors `_resolve_order_db_path` — a separate per-user data file, never the same
    connection (see `docs/adr/0009-safe-reversal-and-scaling.md`)."""
    if settings.reversal_workflow_db_path is not None:
        return Path(settings.reversal_workflow_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "reversal_workflows.sqlite3"


def _resolve_position_baseline_db_path(settings: TradingSettings) -> Path:
    """Mirrors `_resolve_order_db_path`/`_resolve_reversal_workflow_db_path` — a
    separate per-user data file, never the same connection (see
    `docs/adr/0010-position-reconciliation-and-manual-sync.md`)."""
    if settings.position_baseline_db_path is not None:
        return Path(settings.position_baseline_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "position_baselines.sqlite3"


def build_services(settings: TradingSettings) -> ServiceContainer:
    log_info(_logger, "module_load_started", use_mock=settings.use_mock)
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
    market_data_db_path = _resolve_market_data_db_path(settings)
    market_data_connection = create_connection(market_data_db_path, check_same_thread=False)
    log_info(
        _logger,
        "module_loaded",
        module="market_data_db",
        path_configured=settings.market_data_db_path is not None,
        path_basename=market_data_db_path.name,
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
    historical_price_query: HistoricalPriceQueryPort
    if settings.use_mock:
        log_info(_logger, "module_loaded", module="broker_session", kind="mock")
        trade_gateway = MockTradeGateway()
        quote_gateway = MockQuoteGateway()
        broker_session = MockBrokerSession(event_publisher=event_coordinator)
        historical_price_query = MockHistoricalPriceQuery()
    else:
        log_info(_logger, "preflight_checks_started")
        raise_if_any_failed(run_preflight_checks())
        log_info(_logger, "preflight_checks_passed")

        # Imported lazily: this module pulls in pythonnet/CLR-hosting code that only
        # needs to exist for the real (non-mock) branch — see
        # docs/adr/0004-broker-session-architecture.md for why this glue is kept as
        # thin and isolated as possible.
        from tfx_quant.infrastructure.yuanta.spark_api_adapter import SparkApiSessionAdapter
        from tfx_quant.infrastructure.yuanta.spark_historical_price_adapter import (
            SparkHistoricalPriceQueryAdapter,
        )

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
        historical_price_query = SparkHistoricalPriceQueryAdapter(adapter)
        log_info(_logger, "module_loaded", module="broker_session", kind="spark_api")

    instrument_selection = InstrumentSelectionService(
        strategy_state_machine=strategy_state_machine,
        trade_gateway=trade_gateway,
        quote_gateway=quote_gateway,
        instrument_master=instrument_master,
        bar_signal_state_store=bar_signal_state_store,
        clock=clock,
        event_publisher=event_coordinator,
    )

    bar_history_backfill_service = BarHistoryBackfillService(
        event_bus=event_coordinator,
        clock=clock,
        trading_calendar_repository=trading_calendar,
        instrument_master=instrument_master,
        bar_record_repository=bar_record_repository,
        historical_price_query=historical_price_query,
    )

    order_db_path = _resolve_order_db_path(settings)
    order_connection = create_connection(order_db_path, check_same_thread=False)
    log_info(
        _logger,
        "module_loaded",
        module="order_db",
        path_configured=settings.order_db_path is not None,
        path_basename=order_db_path.name,
    )
    order_repository = SqliteOrderRepository(order_connection)

    reversal_workflow_db_path = _resolve_reversal_workflow_db_path(settings)
    reversal_workflow_connection = create_connection(
        reversal_workflow_db_path, check_same_thread=False
    )
    log_info(
        _logger,
        "module_loaded",
        module="reversal_workflow_db",
        path_configured=settings.reversal_workflow_db_path is not None,
        path_basename=reversal_workflow_db_path.name,
    )
    reversal_workflow_repository = SqliteReversalWorkflowRepository(reversal_workflow_connection)

    position_baseline_db_path = _resolve_position_baseline_db_path(settings)
    position_baseline_connection = create_connection(
        position_baseline_db_path, check_same_thread=False
    )
    log_info(
        _logger,
        "module_loaded",
        module="position_baseline_db",
        path_configured=settings.position_baseline_db_path is not None,
        path_basename=position_baseline_db_path.name,
    )
    position_baseline_repository = SqlitePositionBaselineRepository(position_baseline_connection)
    reconciliation_service = PositionReconciliationService(
        trade_gateway=trade_gateway,
        order_repository=order_repository,
        reversal_workflow_repository=reversal_workflow_repository,
        baseline_repository=position_baseline_repository,
        bar_signal_state_store=bar_signal_state_store,
        strategy_state_machine=strategy_state_machine,
        clock=clock,
        event_bus=event_coordinator,
        current_selection=lambda: instrument_selection.current,
        selected_account=lambda: broker_session.selected_account,
    )

    # `reconciliation_service.expected_net_lookup` replaces Feature 06's always-flat
    # `position_lookup` placeholder — see `docs/adr/0010-position-reconciliation-and-
    # manual-sync.md`.
    order_manager = OrderManager(
        trade_gateway=trade_gateway,
        order_repository=order_repository,
        clock=clock,
        id_generator=id_generator,
        event_bus=event_coordinator,
        position_lookup=reconciliation_service.expected_net_lookup,
    )

    reversal_workflow_service = ReversalWorkflowService(
        order_manager=order_manager,
        order_repository=order_repository,
        reversal_workflow_repository=reversal_workflow_repository,
        trade_gateway=trade_gateway,
        clock=clock,
        event_bus=event_coordinator,
        session_healthy=lambda: broker_session.capabilities.is_session_ready,
        market_data_healthy=lambda instrument, contract: (
            not market_data_bar_service.is_stale(instrument, contract)
            and not market_data_bar_service.has_gap(instrument, contract)
        ),
    )
    scaling_service = ScalingService(
        order_manager=order_manager,
        order_repository=order_repository,
        trade_gateway=trade_gateway,
        clock=clock,
    )

    log_info(_logger, "module_load_completed")
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
        bar_history_backfill_service=bar_history_backfill_service,
        order_manager=order_manager,
        reversal_workflow_service=reversal_workflow_service,
        scaling_service=scaling_service,
        reconciliation_service=reconciliation_service,
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


def log_startup_readiness(services: ServiceContainer) -> None:
    """Logs one `readiness_check_completed` event per row of `compute_readiness()`.
    Called once at startup (see `desktop/__main__.py`) rather than from inside
    `compute_readiness()` itself, which the UI also polls on every broker/market-data
    event — logging there would be a per-tick firehose, not a startup snapshot."""
    for label, ready in compute_readiness(services):
        log_info(_logger, "readiness_check_completed", check_name=label, passed=ready)
