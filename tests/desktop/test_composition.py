from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from tfx_quant.application.connectivity.connectivity_monitor import ConnectivityMonitor
from tfx_quant.application.connectivity.gateway_tracking import (
    ConnectivityTrackingBrokerSession,
    ConnectivityTrackingTradeGateway,
)
from tfx_quant.application.events.events import (
    BarBackfillCompleted,
    BrokerSessionInvalidated,
    FillReceived,
)
from tfx_quant.application.market_data.bar_history_backfill_service import (
    BarHistoryBackfillService,
)
from tfx_quant.application.order_management.errors import OrderExposureExceededError
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.position_reconciliation.reconciliation_service import (
    PositionReconciliationService,
)
from tfx_quant.application.reversal_scaling.reversal_service import ReversalWorkflowService
from tfx_quant.application.reversal_scaling.scaling_service import ScalingService
from tfx_quant.application.settings.trading_settings import Environment, validate_startup
from tfx_quant.desktop.composition import (
    auto_select_startup_instrument,
    build_services,
    compute_readiness,
    load_settings,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.errors import PreflightCheckFailed


def _login_request() -> LoginRequest:
    return LoginRequest(
        environment=Environment.TEST,
        user_id="F00000000012345678",
        password=SecretStr("x"),
    )


def test_load_settings_reads_and_validates_json(
    tmp_path: Path, valid_settings_raw: dict[str, Any]
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(valid_settings_raw), encoding="utf-8")

    settings = load_settings(settings_path)

    assert settings.account_alias == "primary"


def test_example_settings_file_is_itself_valid() -> None:
    example_path = (
        Path(__file__).parents[2] / "src" / "tfx_quant" / "desktop" / "settings.example.json"
    )
    settings = load_settings(example_path)
    assert not hasattr(settings, "use_mock")


def test_build_services_wires_logged_out_real_gateway(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert services.trade_gateway.is_logged_in() is False


def test_build_services_wires_a_real_broker_session_without_connecting(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert services.broker_session.capabilities.login is False

    readiness = dict(compute_readiness(services))
    assert readiness["Broker session: login"] is False
    assert readiness["Broker session: trading"] is False
    assert readiness["Broker session: order reports"] is False
    assert readiness["Broker session: queries"] is False
    assert readiness["Market data: yfinance polling"] is True


def test_build_services_raises_actionable_preflight_error_when_use_mock_false(
    valid_settings_raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real branch must fail loudly at startup with an aggregated, actionable
    message for whatever preflight check fails, never silently fall back to a fake —
    per docs/adr/0004-broker-session-architecture.md. The DLL-directory check is forced
    to fail here (pointed at an empty tmp dir, guaranteed to lack `YuantaSparkAPI.dll`)
    so the assertion is deterministic regardless of host state. Credentials are no
    longer checked at startup at all (they're entered on the login screen, not
    available until then — see docs/secrets-management.md)."""
    import tfx_quant.infrastructure.yuanta.preflight as preflight

    monkeypatch.setattr(preflight, "default_api_directory", lambda: tmp_path)

    settings = validate_startup(valid_settings_raw)

    with pytest.raises(PreflightCheckFailed) as exc_info:
        build_services(settings)

    message = str(exc_info.value)
    assert "password" not in message.lower()
    assert "hunter2" not in message


def test_compute_readiness_never_includes_account_number_or_secrets(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    readiness = compute_readiness(services)
    labels = " ".join(label for label, _ in readiness)
    assert "password" not in labels.lower()
    assert "account_no" not in labels.lower()


def test_market_data_bar_service_is_wired_as_the_real_bar_signal_state_store(
    valid_settings_raw: dict[str, Any],
) -> None:
    """ADR 0005 left `NullBarSignalStateStore` as a documented placeholder — Feature 04
    must wire the real service in its place, so switching instruments actually resets
    (activates) the bar aggregator for the newly-selected contract."""
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)

    resolved = services.instrument_selection.resolve_near_month(Instrument.MXF)
    services.instrument_selection.switch_to(resolved)

    bar_service = services.market_data_bar_service
    assert bar_service.forming_bar(resolved.instrument, resolved.contract) is None
    assert bar_service.recent_closed_bars(resolved.instrument, resolved.contract) == ()
    assert bar_service.is_stale(resolved.instrument, resolved.contract) is True
    assert bar_service.has_gap(resolved.instrument, resolved.contract) is False


def test_order_manager_is_wired_into_service_container(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert isinstance(services.order_manager, OrderManager)


def test_order_db_path_resolves_to_configured_path(
    valid_settings_raw: dict[str, Any], tmp_path: Path
) -> None:
    """Mirrors `market_data_db_path`'s resolution — a dedicated, separate SQLite file
    (see docs/adr/0008-order-and-fill-state-machine.md)."""
    settings = validate_startup(valid_settings_raw)
    build_services(settings)
    assert (tmp_path / "orders.sqlite3").exists()


def test_reversal_and_scaling_services_are_wired_into_service_container(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert isinstance(services.reversal_workflow_service, ReversalWorkflowService)
    assert isinstance(services.scaling_service, ScalingService)


def test_reversal_workflow_db_path_resolves_to_configured_path(
    valid_settings_raw: dict[str, Any], tmp_path: Path
) -> None:
    """Mirrors `order_db_path`'s resolution — a dedicated, separate SQLite file (see
    docs/adr/0009-safe-reversal-and-scaling.md)."""
    settings = validate_startup(valid_settings_raw)
    build_services(settings)
    assert (tmp_path / "reversal_workflows.sqlite3").exists()


def test_reconciliation_service_is_wired_into_service_container(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert isinstance(services.reconciliation_service, PositionReconciliationService)


def test_position_baseline_db_path_resolves_to_configured_path(
    valid_settings_raw: dict[str, Any], tmp_path: Path
) -> None:
    """Mirrors `order_db_path`'s resolution — a dedicated, separate SQLite file (see
    docs/adr/0010-position-reconciliation-and-manual-sync.md)."""
    settings = validate_startup(valid_settings_raw)
    build_services(settings)
    assert (tmp_path / "position_baselines.sqlite3").exists()


def test_order_manager_position_lookup_is_the_reconciliation_service_not_a_flat_placeholder(
    valid_settings_raw: dict[str, Any],
) -> None:
    """Feature 06 left `OrderManager`'s `position_lookup` as an always-flat placeholder
    documented as "Feature 08's job" — this proves the replacement is actually wired by
    observing `OrderManager`'s own behavior: a candidate order that would only exceed
    the exposure cap once the reconciled baseline is accounted for must be rejected.

    Publishes `FillReceived` directly on `services.event_coordinator` rather than via
    `services.trade_gateway.simulate_fill()`: `build_services()`'s mock branch never
    wires an `event_publisher` into `MockTradeGateway` (nothing in production code
    calls its `simulate_*` scripting helpers — those exist purely for tests that build
    their own standalone gateway, as every `application.order_management`/
    `application.reversal_scaling` test does), so this drives the same seam the real
    adapter would: an event landing on the shared coordinator."""
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    resolved = services.instrument_selection.resolve_near_month(Instrument.MXF)
    account = TradingAccount(branch_id="0001", account_no="1234567")

    submit_request = OrderRequest(
        account=account,
        instrument=resolved.instrument,
        contract=resolved.contract,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("18500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
        idempotency_key="baseline-establishing-fill",
        workflow_id="wiring-test",
        reason="composition wiring test",
    )
    intent = services.order_manager.submit(submit_request)
    fill = Fill(
        client_order_id=intent.client_order_id,
        instrument=resolved.instrument,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("18500")),
        at=services.clock.now(),
        broker_fill_no="F-wiring",
        broker_seq_no=1,
    )
    services.event_coordinator.publish(FillReceived(at=fill.at, fill=fill))
    # Events are only dispatched once EventCoordinator's consumer thread is running;
    # `stop(timeout=...)` drains everything already queued before joining.
    services.event_coordinator.start()
    services.event_coordinator.stop(timeout=2)

    assert services.reconciliation_service.expected_net_lookup(
        account, resolved.instrument, resolved.contract
    ) == NetPosition(1)

    second_request = OrderRequest(
        account=account,
        instrument=resolved.instrument,
        contract=resolved.contract,
        side=Side.BUY,
        quantity=Quantity(2),
        price=Price(Decimal("18500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
        idempotency_key="over-cap-attempt",
        workflow_id="wiring-test",
        reason="composition wiring test",
    )
    with pytest.raises(OrderExposureExceededError):
        services.order_manager.submit(second_request)


def test_bar_history_backfill_service_is_wired_into_service_container(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert isinstance(services.bar_history_backfill_service, BarHistoryBackfillService)


def test_yfinance_backfill_readiness_row_is_healthy_with_no_active_selection(
    valid_settings_raw: dict[str, Any],
) -> None:
    """Nothing is selected yet at composition time, so the yfinance-backfill degraded
    check (which only ever inspects the *currently active* contract) has nothing to
    report — same "nothing failed yet is reported healthy" posture every other
    readiness row in this codebase takes."""
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    readiness = dict(compute_readiness(services))
    assert readiness["Market data: yfinance backfill"] is True


def test_default_runtime_has_an_active_yfinance_market_data_mapping(
    valid_settings_raw: dict[str, Any],
) -> None:
    """The desktop's default selection can start yfinance before broker login."""
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)

    auto_select_startup_instrument(services)
    services.event_coordinator.start()
    services.event_coordinator.stop(timeout=2)

    assert services.instrument_selection.current is not None
    assert services.bar_history_backfill_service.is_degraded() is False


def test_default_runtime_persists_only_yfinance_bars(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    completed = threading.Event()
    services.event_coordinator.subscribe(BarBackfillCompleted, lambda _event: completed.set())
    services.event_coordinator.start()
    try:
        auto_select_startup_instrument(services)
        services.market_data_bar_service.start()
        services.bar_history_backfill_service.start()
        assert completed.wait(timeout=15)

        selected = services.instrument_selection.current
        assert selected is not None
        today = services.clock.now().value.date()
        records = services.market_data_bar_service.query_history(
            selected.instrument,
            selected.contract,
            start_date=today - timedelta(days=7),
            end_date=today,
        )
        assert records
        assert all("YFINANCE" in record.source.value for record in records)
    finally:
        services.bar_history_backfill_service.stop()
        services.market_data_bar_service.stop()
        services.event_coordinator.stop(timeout=2)


def test_yahoo_ticker_mapping_path_is_honored(
    valid_settings_raw: dict[str, Any], tmp_path: Path
) -> None:
    """A configured `yahoo_ticker_mapping_path` must actually be the file
    `BarHistoryBackfillService` consults — proven behaviorally (via `is_degraded()`
    after an instrument switch) rather than by reaching into composition internals."""
    mapping_path = tmp_path / "yahoo_ticker_mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "mappings": [
                    {
                        "instrument": "MXF",
                        "contract_year": 2026,
                        "contract_month": 9,
                        "yahoo_ticker": "TESTTICKER=F",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    valid_settings_raw["yahoo_ticker_mapping_path"] = str(mapping_path)
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)

    resolved = services.instrument_selection.resolve_near_month(Instrument.MXF)
    services.instrument_selection.switch_to(resolved)
    # `BarHistoryBackfillService` only learns the active contract via its
    # `InstrumentSwitchCompleted` subscription (unlike `MarketDataBarService.clear()`,
    # which `switch_to()` calls synchronously) — the event coordinator must actually be
    # dispatching for that handler to fire.
    services.event_coordinator.start()
    services.event_coordinator.stop(timeout=2)

    # A configured mapping means "is_degraded" no longer reports the missing-ticker
    # condition (mock mode's yahoo_history_query always returns empty, so there is
    # nothing to conflict on either).
    assert services.bar_history_backfill_service.is_degraded() is False


def test_connectivity_monitor_is_wired_into_service_container(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert isinstance(services.connectivity_monitor, ConnectivityMonitor)


def test_broker_session_and_trade_gateway_are_connectivity_tracking_wrappers(
    valid_settings_raw: dict[str, Any],
) -> None:
    """Feature 09's gateway wrappers must sit in front of every other service's
    `broker_session`/`trade_gateway` dependency — see `docs/adr/0011-connectivity-
    reconnect-and-safe-pause.md`'s wiring-order note."""
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert isinstance(services.broker_session, ConnectivityTrackingBrokerSession)
    assert isinstance(services.trade_gateway, ConnectivityTrackingTradeGateway)


def test_full_disconnect_after_login_drives_a_connectivity_safe_pause_via_composition(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    services.strategy_state_machine.transition(StrategyState.STARTING)
    services.strategy_state_machine.transition(StrategyState.RUNNING)

    services.event_coordinator.publish(
        BrokerSessionInvalidated(at=Timestamp.now(), reason="composition wiring test: 斷線")
    )
    services.event_coordinator.start()
    services.event_coordinator.stop(timeout=2)

    assert services.strategy_state_machine.state is StrategyState.PAUSED_SAFE
    record = services.connectivity_monitor.current_pause()
    assert record is not None
    assert record.detail == "composition wiring test: 斷線"
    readiness = dict(compute_readiness(services))
    assert readiness["Connectivity: no unresolved safe-pause"] is False
