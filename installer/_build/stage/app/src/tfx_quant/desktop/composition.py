"""The composition root — the one place concrete services get wired together.

Constructs the Yuanta trading and quote gateways behind application-layer protocols so
tests can substitute implementations without touching this module.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from tfx_quant.application.connectivity.connectivity_monitor import ConnectivityMonitor
from tfx_quant.application.connectivity.gateway_tracking import (
    ConnectivityTrackingBrokerSession,
    ConnectivityTrackingTradeGateway,
)
from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.instrument_selection.errors import InstrumentSelectionError
from tfx_quant.application.instrument_selection.instrument_selection_service import (
    InstrumentSelectionService,
)
from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.ports.bar_signal_state import BarSignalStateStore
from tfx_quant.application.ports.broker_session import IBrokerSession
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.identity import IdGenerator
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.quote_gateway import QuoteGateway
from tfx_quant.application.ports.trading_calendar import TradingCalendarRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.application.position_reconciliation.reconciliation_service import (
    PositionReconciliationService,
)
from tfx_quant.application.reversal_scaling.reversal_service import ReversalWorkflowService
from tfx_quant.application.reversal_scaling.scaling_service import ScalingService
from tfx_quant.application.risk.risk_supervisor import RiskSupervisor
from tfx_quant.application.settings.trading_settings import (
    Environment,
    TradingSettings,
    validate_startup,
)
from tfx_quant.application.strategy_signal.signal_engine_service import (
    StrategySignalEngineService,
)
from tfx_quant.application.trade_reports import (
    FillLedgerService,
    PositionValuationService,
    TradeReportFacade,
    TradeReportService,
    fee_model_from_settings,
)
from tfx_quant.desktop.quote_runtime import QuoteRuntime
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.strategy_state import StrategyStateMachine
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trading_calendar import TradingCalendar
from tfx_quant.infrastructure.clock import SystemClock
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.market_data.trading_calendar_repository import (
    JsonTradingCalendarRepository,
)
from tfx_quant.infrastructure.yuanta.instrument_master_repository import (
    JsonInstrumentMasterRepository,
)
from tfx_quant.infrastructure.yuanta.preflight import raise_if_any_failed, run_preflight_checks
from tfx_quant.persistence.sqlite_bar_record_repository import SqliteBarRecordRepository
from tfx_quant.persistence.sqlite_connection import create_connection
from tfx_quant.persistence.sqlite_eod_flatten_workflow_repository import (
    SqliteEodFlattenWorkflowRepository,
)
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository
from tfx_quant.persistence.sqlite_market_event_repository import SqliteMarketEventRepository
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository
from tfx_quant.persistence.sqlite_position_baseline_repository import (
    SqlitePositionBaselineRepository,
)
from tfx_quant.persistence.sqlite_reversal_workflow_repository import (
    SqliteReversalWorkflowRepository,
)
from tfx_quant.telemetry import get_logger, log_error, log_info, log_warning
from tfx_quant.telemetry.audit import AuditTimelineStep, read_workflow_timeline

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


class _CompositeBarSignalStateStore:
    """Fans `clear()` out to every registered store — combines `MarketDataBarService`'s
    own bar-aggregation-state clear with `StrategySignalEngineService`'s strategy-
    position-state clear behind the single `BarSignalStateStore` seam
    `InstrumentSelectionService`/`PositionReconciliationService` depend on. Stores are
    registered via `add()` after construction (same forward-reference-via-closure trick
    as `order_summary_provider`/`position_summary_provider` below) because
    `StrategySignalEngineService` needs `order_manager`, which this function only builds
    later — safe because nothing calls `clear()` until well after `build_services()`
    returns."""

    def __init__(self) -> None:
        self._stores: list[BarSignalStateStore] = []

    def add(self, store: BarSignalStateStore) -> None:
        self._stores.append(store)

    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        for store in self._stores:
            store.clear(instrument, contract)


@dataclass
class ServiceContainer:
    settings: TradingSettings
    clock: Clock
    id_generator: IdGenerator
    trade_gateway: TradeGatewayPort
    broker_session: IBrokerSession
    event_coordinator: EventCoordinator
    strategy_state_machine: StrategyStateMachine
    instrument_master: InstrumentMasterRepository
    instrument_selection: InstrumentSelectionService
    quote_runtime: QuoteRuntime
    order_manager: OrderManager
    reversal_workflow_service: ReversalWorkflowService
    scaling_service: ScalingService
    reconciliation_service: PositionReconciliationService
    connectivity_monitor: ConnectivityMonitor
    signal_engine_service: StrategySignalEngineService
    risk_supervisor: RiskSupervisor
    trade_report_service: TradeReportService
    trade_report_facade: TradeReportFacade
    fill_ledger_service: FillLedgerService
    position_valuation_service: PositionValuationService
    order_repository: OrderRepository
    audit_timeline_reader: Callable[[str], tuple[AuditTimelineStep, ...]]
    simulation: bool = False
    """`True` in the 測試環境 (`settings.environment is Environment.TEST`): the trade
    adapter is the local simulator and never sends anything to a server; market data is
    still the real Yuanta quote feed. `False` in 正式環境 (`PRODUCTION`)."""


@dataclass(frozen=True, slots=True)
class RuntimeOverrides:
    """Test-only adapter seam. Normal startup never supplies this — `build_services`
    picks the trade adapter from `settings.environment` (mock in TEST, real
    `LegacyBroker` in PRODUCTION) and the real `YuantaQuoteComHost` for market data.
    Tests inject a fake quote host / fake clock / a scripted mock broker here instead.
    """

    clock: Clock | None = None
    id_generator: IdGenerator | None = None
    broker_factory: Callable[[EventCoordinator], tuple[TradeGatewayPort, IBrokerSession]] | None = (
        None
    )
    quote_gateway_factory: (
        Callable[[Callable[[RawMarketEvent], None], Callable[[MarketDataGap], None]], QuoteGateway]
        | None
    ) = None


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
        max_net_lots=settings.max_net_lots,
        instrument_master_path_configured=settings.instrument_master_path is not None,
        trading_calendar_path_configured=settings.trading_calendar_path is not None,
        market_data_db_path_configured=settings.market_data_db_path is not None,
        order_db_path_configured=settings.order_db_path is not None,
        reversal_workflow_db_path_configured=settings.reversal_workflow_db_path is not None,
        position_baseline_db_path_configured=settings.position_baseline_db_path is not None,
        eod_flatten_workflow_db_path_configured=settings.eod_flatten_workflow_db_path is not None,
        fill_ledger_db_path_configured=settings.fill_ledger_db_path is not None,
        simulation_fee_model_configured=settings.simulation_fee_model is not None,
    )
    return settings


def _resolve_instrument_master_path(settings: TradingSettings) -> Path:
    if settings.instrument_master_path is None:
        return _DEFAULT_INSTRUMENT_MASTER_PATH
    return Path(settings.instrument_master_path)


def _resolve_order_symbol(instrument_master: InstrumentMasterRepository, order: object) -> str:
    from tfx_quant.domain.order import Order

    if not isinstance(order, Order):
        raise TypeError("order must be an Order")
    entry = instrument_master.get(order.instrument, order.contract)
    if entry is None or not entry.tradable:
        raise ValueError("委託商品不在受控商品主檔內，或目前不可交易")
    return entry.vendor_symbol


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


def _resolve_eod_flatten_workflow_db_path(settings: TradingSettings) -> Path:
    """Mirrors `_resolve_reversal_workflow_db_path`/`_resolve_position_baseline_db_path`
    — a separate per-user data file, never the same connection."""
    if settings.eod_flatten_workflow_db_path is not None:
        return Path(settings.eod_flatten_workflow_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "eod_flatten_workflows.sqlite3"


def _resolve_audit_db_path(settings: TradingSettings) -> Path:
    if settings.audit_db_path is not None:
        return Path(settings.audit_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "logs" / "audit.sqlite3"


def _resolve_fill_ledger_db_path(settings: TradingSettings) -> Path:
    """Mirrors `_resolve_order_db_path` — a separate per-user data file for the
    append-only execution ledger (`application.trade_reports.fill_ledger_service`), never
    the same connection as any other `*_db_path`."""
    if settings.fill_ledger_db_path is not None:
        return Path(settings.fill_ledger_db_path)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "tfx_quant" / "fill_ledger.sqlite3"


def _current_expected_net(
    instrument_selection: InstrumentSelectionService,
    broker_session: IBrokerSession,
    reconciliation_service: PositionReconciliationService,
) -> NetPosition | None:
    """`ConnectivityMonitor`'s `position_summary_provider` — the "當時...持倉摘要" a
    `SafePauseRecord` carries. `None` when there's no current selection/account to
    summarize yet (e.g. a pause triggered before the operator has picked an
    instrument), same "nothing to report yet" posture as every other optional summary
    field in this codebase."""
    selection = instrument_selection.current
    account = broker_session.selected_account
    if selection is None or account is None:
        return None
    return reconciliation_service.expected_net_lookup(
        account, selection.instrument, selection.contract
    )


def build_services(
    settings: TradingSettings, *, runtime_overrides: RuntimeOverrides | None = None
) -> ServiceContainer:
    overrides = runtime_overrides or RuntimeOverrides()
    is_test_env = settings.environment is Environment.TEST
    broker_mode = (
        "test_simulator" if (is_test_env or overrides.broker_factory is not None) else "yuanta_ocx"
    )
    log_info(
        _logger,
        "module_load_started",
        broker_mode=broker_mode,
        environment=settings.environment.value,
    )
    clock: Clock = overrides.clock or SystemClock()
    id_generator: IdGenerator = overrides.id_generator or UuidIdGenerator()
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
    market_event_repository = SqliteMarketEventRepository(
        create_connection(market_data_db_path, check_same_thread=False)
    )

    trade_gateway: TradeGatewayPort
    broker_session: IBrokerSession
    # Trade adapter selection is driven purely by `settings.environment`:
    #   PRODUCTION -> the real Yuanta OCX broker (real trade server). A preflight failure
    #                 fails loudly here, never a silent mock fallback (ADR 0004).
    #   TEST       -> the local broker simulator: it never sends anything to any server.
    #                 This *replaces* the old "log in to the trading API's UAT/test
    #                 environment" flow — there is no test trade server any more.
    # `runtime_overrides.broker_factory` is a test-only seam that pre-empts both.
    if overrides.broker_factory is not None:
        trade_gateway, broker_session = overrides.broker_factory(event_coordinator)
    elif is_test_env:
        from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
        from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway

        trade_gateway = MockTradeGateway(event_publisher=event_coordinator)
        broker_session = MockBrokerSession(event_publisher=event_coordinator)
    else:
        log_info(_logger, "preflight_checks_started")
        raise_if_any_failed(run_preflight_checks())
        log_info(_logger, "preflight_checks_passed")

        # Imported lazily: the broker's ActiveX host only exists after preflight.
        import wx

        from tfx_quant.infrastructure.yuanta.legacy_broker import LegacyBroker

        adapter = LegacyBroker(
            event_publisher=event_coordinator,
            symbol_resolver=lambda order: _resolve_order_symbol(instrument_master, order),
            # `ConnectivityMonitor` retries a dropped session from a `threading.Timer`
            # thread, but the OCX may only be created and closed on the wx UI thread.
            ui_dispatch=wx.CallAfter,
        )
        trade_gateway = adapter
        broker_session = adapter
    if is_test_env and overrides.broker_factory is None:
        _assert_test_env_broker_is_local_simulator(trade_gateway, broker_session)
    log_info(_logger, "module_loaded", module="broker_session", kind=broker_mode)

    bar_signal_state_store = _CompositeBarSignalStateStore()

    # `connectivity_monitor` is built against the *raw* `broker_session` (its own
    # reconnect attempts call `IBrokerSession.start()` directly — see
    # `ConnectivityMonitor.__init__`'s docstring) before `trade_gateway`/
    # `broker_session` are rebound to their connectivity-tracking wrappers below, so
    # every other service in this function (instrument selection, order management,
    # position reconciliation, reversal/scaling) sees the wrapped versions uniformly.
    # `order_summary_provider`/`position_summary_provider` are forward references to
    # `order_repository`/`instrument_selection`/`broker_session`, all assigned further
    # down this same function — safe because Python closures resolve names at call
    # time, and nothing calls these providers until well after `build_services()`
    # returns (a safe-pause trigger, at the earliest) — see
    # docs/adr/0011-connectivity-reconnect-and-safe-pause.md.
    connectivity_monitor = ConnectivityMonitor(
        broker_session=broker_session,
        strategy_state_machine=strategy_state_machine,
        clock=clock,
        event_bus=event_coordinator,
        order_summary_provider=lambda: len(order_repository.list_active()),
        position_summary_provider=lambda: _current_expected_net(
            instrument_selection, broker_session, reconciliation_service
        ),
    )
    trade_gateway = ConnectivityTrackingTradeGateway(trade_gateway, connectivity_monitor)
    broker_session = ConnectivityTrackingBrokerSession(broker_session, connectivity_monitor)

    instrument_selection = InstrumentSelectionService(
        strategy_state_machine=strategy_state_machine,
        trade_gateway=trade_gateway,
        instrument_master=instrument_master,
        bar_signal_state_store=bar_signal_state_store,
        clock=clock,
        event_publisher=event_coordinator,
        broker_session_ready=lambda: broker_session.capabilities.is_session_ready,
    )

    def quote_gateway_factory(
        on_event: Callable[[RawMarketEvent], None], on_gap: Callable[[MarketDataGap], None]
    ) -> QuoteGateway:
        # Market data is the real Yuanta quote feed in BOTH environments — only the trade
        # adapter differs. Tests substitute a fake host via `runtime_overrides`.
        if overrides.quote_gateway_factory is not None:
            return overrides.quote_gateway_factory(on_event, on_gap)
        from tfx_quant.infrastructure.yuanta.quote_com_host import YuantaQuoteComHost

        return YuantaQuoteComHost(on_event, on_gap)

    quote_runtime = QuoteRuntime(
        clock=clock,
        event_bus=event_coordinator,
        selection=instrument_selection,
        instrument_master=instrument_master,
        trading_calendar=trading_calendar,
        bar_repository=bar_record_repository,
        event_repository=market_event_repository,
        gateway_factory=quote_gateway_factory,
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

    # Must run after `reconciliation_service`/`order_manager` have all subscribed their
    # own `BrokerSessionReady` handlers — see `ConnectivityMonitor.
    # attach_reconnect_reconciliation_watcher`'s docstring.
    connectivity_monitor.attach_reconnect_reconciliation_watcher()

    # Feature 11 — the append-only execution ledger and P&L reporting. `FillLedgerService`
    # subscribes to `FillReceived` *after* `OrderManager` (built just above) so it always
    # reads an order intent whose `broker_order_no` is already set; it never mutates order
    # state. Its own dedicated SQLite file/connection, never shared with any other
    # repository (same lock-hazard reasoning as every other `*_db_path`).
    fill_ledger_db_path = _resolve_fill_ledger_db_path(settings)
    fill_ledger_connection = create_connection(fill_ledger_db_path, check_same_thread=False)
    log_info(
        _logger,
        "module_loaded",
        module="fill_ledger_db",
        path_configured=settings.fill_ledger_db_path is not None,
        path_basename=fill_ledger_db_path.name,
    )
    fill_ledger_repository = SqliteFillLedgerRepository(fill_ledger_connection)
    trade_report_service = TradeReportService(fill_ledger_repository)
    report_calendar = TradingCalendar(
        trading_calendar.get_holidays(), trading_calendar.get_early_closes()
    )

    def _multiplier_lookup(instrument: Instrument, contract: ContractMonth) -> Decimal:
        entry = instrument_master.get(instrument, contract)
        if entry is None:
            raise ValueError(
                f"instrument master has no entry for {instrument.value} {contract.code}"
            )
        return entry.multiplier

    def _trading_day_for(
        instant: Timestamp, instrument: Instrument, contract: ContractMonth
    ) -> date:
        entry = instrument_master.get(instrument, contract)
        if entry is not None:
            resolved = report_calendar.boundary_containing(instant, entry)
            if resolved is not None:
                return resolved[2]
        return instant.value.date()

    simulation_flag = is_test_env
    trade_report_facade = TradeReportFacade(
        trade_report_service, fill_ledger_repository, _multiplier_lookup
    )
    fill_ledger_service = FillLedgerService(
        report_service=trade_report_service,
        order_repository=order_repository,
        trading_day_resolver=_trading_day_for,
        multiplier_lookup=_multiplier_lookup,
        fee_model=fee_model_from_settings(settings.simulation_fee_model),
        event_bus=event_coordinator,
        simulation=simulation_flag,
        source="SIMULATION" if simulation_flag else "YUANTA_OCX",
    )
    position_valuation_service = PositionValuationService(
        fill_ledger=fill_ledger_repository,
        multiplier_lookup=_multiplier_lookup,
        clock=clock,
        event_bus=event_coordinator,
        simulation=simulation_flag,
    )
    _audit_db_path = _resolve_audit_db_path(settings)

    def _audit_timeline_reader(workflow_id: str) -> tuple[AuditTimelineStep, ...]:
        return read_workflow_timeline(_audit_db_path, workflow_id)

    eod_flatten_workflow_db_path = _resolve_eod_flatten_workflow_db_path(settings)
    eod_flatten_workflow_connection = create_connection(
        eod_flatten_workflow_db_path, check_same_thread=False
    )
    log_info(
        _logger,
        "module_loaded",
        module="eod_flatten_workflow_db",
        path_configured=settings.eod_flatten_workflow_db_path is not None,
        path_basename=eod_flatten_workflow_db_path.name,
    )
    eod_flatten_workflow_repository = SqliteEodFlattenWorkflowRepository(
        eod_flatten_workflow_connection
    )

    # Feature 10 — the independent, highest-priority risk supervisor. Built here (right
    # after `order_manager`) so `signal_engine_service` below can be wired against its
    # `validate_entry_window` gate.
    risk_supervisor = RiskSupervisor(
        order_manager=order_manager,
        order_repository=order_repository,
        eod_flatten_workflow_repository=eod_flatten_workflow_repository,
        trade_gateway=trade_gateway,
        clock=clock,
        event_bus=event_coordinator,
        strategy_state_machine=strategy_state_machine,
        session_healthy=lambda: broker_session.capabilities.is_session_ready,
        market_data_healthy=lambda _instrument, _contract: quote_runtime.state.value == "LOGGED_ON",
        current_selection=lambda: instrument_selection.current,
        selected_account=lambda: broker_session.selected_account,
    )

    reversal_workflow_service = ReversalWorkflowService(
        order_manager=order_manager,
        order_repository=order_repository,
        reversal_workflow_repository=reversal_workflow_repository,
        trade_gateway=trade_gateway,
        clock=clock,
        event_bus=event_coordinator,
        session_healthy=lambda: broker_session.capabilities.is_session_ready,
        market_data_healthy=lambda _instrument, _contract: quote_runtime.state.value == "LOGGED_ON",
    )
    scaling_service = ScalingService(
        order_manager=order_manager,
        order_repository=order_repository,
        trade_gateway=trade_gateway,
        clock=clock,
    )

    # Feature 05 — the only automatic driver of `OrderManager.submit()` in this
    # codebase; see `application.strategy_signal.signal_engine_service`'s module
    # docstring for why it submits directly rather than through `scaling_service`.
    signal_engine_service = StrategySignalEngineService(
        order_manager=order_manager,
        order_repository=order_repository,
        clock=clock,
        event_bus=event_coordinator,
        selected_account=lambda: broker_session.selected_account,
        risk_gate=risk_supervisor.validate_entry_window,
    )
    bar_signal_state_store.add(signal_engine_service)

    log_info(_logger, "module_load_completed")
    return ServiceContainer(
        settings=settings,
        clock=clock,
        id_generator=id_generator,
        trade_gateway=trade_gateway,
        broker_session=broker_session,
        event_coordinator=event_coordinator,
        strategy_state_machine=strategy_state_machine,
        instrument_master=instrument_master,
        instrument_selection=instrument_selection,
        quote_runtime=quote_runtime,
        order_manager=order_manager,
        reversal_workflow_service=reversal_workflow_service,
        scaling_service=scaling_service,
        reconciliation_service=reconciliation_service,
        connectivity_monitor=connectivity_monitor,
        signal_engine_service=signal_engine_service,
        risk_supervisor=risk_supervisor,
        trade_report_service=trade_report_service,
        trade_report_facade=trade_report_facade,
        fill_ledger_service=fill_ledger_service,
        position_valuation_service=position_valuation_service,
        order_repository=order_repository,
        audit_timeline_reader=_audit_timeline_reader,
        simulation=simulation_flag,
    )


def auto_select_startup_instrument(services: ServiceContainer) -> None:
    """Resolves and switches to the settings-configured instrument/contract once, right
    at startup, so the local quote runtime can display the automatically resolved near
    month without an operator click. The settings file's `selected_instrument` is the
    initial market-view choice; later UI switches also resolve the near month automatically.

    Best-effort: logs and leaves the selection empty on failure (missing master entry,
    or a blocked switch) rather than raising and
    crashing startup. The readiness screen and instrument panel already surface an
    unselected state clearly, same as if the operator simply hadn't clicked yet.
    """
    settings = services.settings
    service = services.instrument_selection
    try:
        resolved = service.resolve_near_month(settings.selected_instrument)
        service.switch_to(resolved)
        log_info(
            _logger,
            "startup_instrument_auto_selected",
            instrument=resolved.instrument.value,
            contract=resolved.contract.code,
        )
    except InstrumentSelectionError as exc:
        log_warning(
            _logger,
            "startup_instrument_auto_select_failed",
            instrument=settings.selected_instrument.value,
            contract_selection_mode=settings.contract_selection_mode.value,
            error=str(exc),
        )


def compute_readiness(services: ServiceContainer) -> list[tuple[str, bool]]:
    """Per-module readiness for the startup diagnostics screen.

    Never includes credentials or raw account numbers — only booleans/labels. The four
    `SessionCapabilities` rows are listed independently on purpose: the implementation
    prompt explicitly forbids collapsing "logged in" into "can trade" — see
    `application/ports/broker_session.py`. The separately authenticated Yuanta quote
    connection has its own readiness row and is never inferred from trading login.
    """
    capabilities = services.broker_session.capabilities
    rows = [
        ("Settings loaded and validated", True),
        ("Event coordinator running", services.event_coordinator.is_running),
        ("Broker session: login", capabilities.login),
        ("Broker session: trading", capabilities.trading),
        ("Broker session: order reports", capabilities.order_reports),
        ("Broker session: queries", capabilities.queries),
        ("Market data: Yuanta quote login", services.quote_runtime.state.value == "LOGGED_ON"),
        ("Connectivity: no unresolved safe-pause", _connectivity_pause_resolved(services)),
    ]
    if services.simulation:
        rows.insert(0, ("測試環境：模擬下單（不會送出真單）・真實行情", True))
        rows.insert(1, ("交易 adapter：本機模擬（fail-closed 已驗證）", True))
    return rows


class TestEnvStartupError(RuntimeError):
    """測試環境 startup refused because a real-order path could not be ruled out."""

    __test__ = False  # not a pytest test class despite the name


_TEST_ENV_LOGIN_USER_ID = "TEST-SIMULATION"


def environment_switch_blocked_reason(services: ServiceContainer) -> str | None:
    """`None` when it is safe to tear the container down and rebuild it for the other
    執行環境 (模擬下單 ↔ 正式下單), otherwise a human-readable reason it is refused.

    A rebuild is only safe while nothing is actually trading: the strategy must be
    stopped/faulted and there must be no active order occupying a workflow slot. Open
    positions are covered transitively — a position cannot exist without a fill, and the
    operator cannot have logged the broker in without first being on the readiness
    screen where this check runs."""
    state = services.strategy_state_machine.state.value
    if state not in ("STOPPED", "FAULTED"):
        return f"策略目前為 {state}，請先停止策略再切換執行環境"
    if services.order_repository.list_active():
        return "尚有活動委託，請先處理完畢再切換執行環境"
    return None


def _assert_test_env_broker_is_local_simulator(
    trade_gateway: TradeGatewayPort, broker_session: IBrokerSession
) -> None:
    """Fail closed: in the 測試環境 the trade adapter must be the local simulator that
    never opens a socket. `MockBrokerSession`/`MockTradeGateway` are the only broker
    objects with no network path at all."""
    from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
    from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway

    trade = getattr(trade_gateway, "_inner", trade_gateway)
    session = getattr(broker_session, "_inner", broker_session)
    if not isinstance(trade, MockTradeGateway) or not isinstance(session, MockBrokerSession):
        raise TestEnvStartupError(
            "測試環境 fail-closed: trade adapter is "
            f"{type(trade).__name__}/{type(session).__name__}, not the local simulator"
        )


def assert_test_env_fail_closed(services: ServiceContainer) -> None:
    """Public re-check used by startup and the acceptance tests."""
    if not services.simulation:
        raise TestEnvStartupError("測試環境 fail-closed: services.simulation is False")
    _assert_test_env_broker_is_local_simulator(services.trade_gateway, services.broker_session)
    session = getattr(services.broker_session, "_inner", services.broker_session)
    bad = [
        r.user_id
        for r in getattr(session, "start_calls", [])
        if r.user_id != _TEST_ENV_LOGIN_USER_ID
    ]
    if bad:
        raise TestEnvStartupError(f"測試環境 fail-closed: non-simulator login user id {bad!r}")


def start_test_env_broker_session(services: ServiceContainer) -> None:
    """Start the local mock broker session. The reserved `TEST-SIMULATION` user id is the
    only login it ever sees — there are no real trade credentials in the 測試環境."""
    from pydantic import SecretStr

    from tfx_quant.application.ports.broker_session import LoginRequest

    assert_test_env_fail_closed(services)
    services.broker_session.start(
        LoginRequest(
            environment=Environment.TEST,
            user_id=_TEST_ENV_LOGIN_USER_ID,
            password=SecretStr("TEST-SIMULATION-ONLY"),
        )
    )
    assert_test_env_fail_closed(services)


def start_test_env_quote_login(services: ServiceContainer, user_id: str, password: str) -> None:
    """Log the *real* Yuanta quote feed in for the 測試環境. The credentials reach the
    quote OCX only; a failure re-raises after stopping the runtime (fail-closed)."""
    from pydantic import SecretStr

    assert_test_env_fail_closed(services)
    user_id = user_id.strip()
    if not user_id or not password:
        raise ValueError("行情登入帳號與密碼為必填")
    services.quote_runtime.stop()
    try:
        services.quote_runtime.start(user_id, SecretStr(password))
    except Exception:
        services.quote_runtime.stop()
        raise


def _connectivity_pause_resolved(services: ServiceContainer) -> bool:
    """True when there has never been a connectivity safe-pause this session, or the
    most recent one has already been reconciled (a fresh `BrokerSessionReady` plus the
    synchronous order/fill/position reconnect-reconciliation fan-out — see
    `ConnectivityMonitor.attach_reconnect_reconciliation_watcher`). Deliberately does
    *not* mean "safe to resume" — `StrategyState` staying `PAUSED_SAFE` until a human
    restarts the strategy is unaffected either way; this only distinguishes "actively
    broken" from "resolved, awaiting a manual restart" for this diagnostics row."""
    record = services.connectivity_monitor.current_pause()
    return record is None or record.reconciled


def log_startup_readiness(services: ServiceContainer) -> None:
    """Logs one `readiness_check_completed` event per row of `compute_readiness()`.
    Called once at startup (see `desktop/__main__.py`) rather than from inside
    `compute_readiness()` itself, which the UI also polls on every broker/market-data
    event — logging there would be a per-tick firehose, not a startup snapshot."""
    for label, ready in compute_readiness(services):
        log_info(_logger, "readiness_check_completed", check_name=label, passed=ready)
