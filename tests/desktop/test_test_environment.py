"""測試環境 acceptance — `settings.environment == TEST`.

Real Yuanta quote *shape* (a fake quote host stands in for the 32-bit OCX), the local
broker simulator for all execution, and no trade-API path at all. Proves: the trade API
is never logged in or connected, every execution comes from the simulator, the *same*
production read models expose orders / positions / P&L / trade report, every record is
marked `simulation=true`, and a restart rebuilds an identical report.

Each test is also one line of the customer sign-off checklist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from tfx_quant.application.order_management.order_manager import OrderRequest
from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.application.settings.trading_settings import validate_startup
from tfx_quant.desktop.composition import (
    RuntimeOverrides,
    ServiceContainer,
    TestEnvStartupError,
    assert_test_env_fail_closed,
    auto_select_startup_instrument,
    build_services,
    compute_readiness,
    environment_switch_blocked_reason,
    start_test_env_broker_session,
    start_test_env_quote_login,
)
from tfx_quant.desktop.view_models.orders_view_model import build_orders_view
from tfx_quant.desktop.view_models.pnl_view_model import build_pnl_view
from tfx_quant.desktop.view_models.positions_view_model import build_positions_view
from tfx_quant.desktop.view_models.trade_report_view_model import (
    build_trade_report_view,
    drill_down,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.quantity import Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.valuation import PriceQuality
from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway

pytestmark = pytest.mark.test_env

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_WIDE = (date(2000, 1, 1), date(2100, 1, 1))


class FakeYuantaQuoteComHost:
    """Stand-in for the real 32-bit `YuantaQuoteComHost` — documented `QuoteGateway`
    surface only. A test double, never a production quote source."""

    instances: list[FakeYuantaQuoteComHost] = []

    def __init__(
        self,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
    ) -> None:
        self.on_event, self.on_gap = on_event, on_gap
        self.state = QuoteConnectionState.IDLE
        self.subscriptions: list[str] = []
        self.connect_calls: list[tuple[str, str]] = []
        FakeYuantaQuoteComHost.instances.append(self)

    def connect(
        self,
        user_id: str,
        password: SecretStr,
        host: str,
        port: int,
        request_type: QuoteRequestType,
    ) -> None:
        del host, port, request_type
        self.connect_calls.append((user_id, password.get_secret_value()))
        self.state = QuoteConnectionState.LOGGED_ON

    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None:
        del request_type, mode
        self.subscriptions.append(symbol)

    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None:
        del request_type
        if symbol in self.subscriptions:
            self.subscriptions.remove(symbol)

    def stop(self) -> None:
        self.state = QuoteConnectionState.STOPPED

    def feed(self, event: RawMarketEvent) -> None:
        self.on_event(event)


@pytest.fixture(autouse=True)
def _isolated_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    FakeYuantaQuoteComHost.instances.clear()
    return tmp_path


class _FixedClock:
    """A Taipei-daytime instant so the quote session window is open and the near-month
    MXF contract resolves (see `instrument_master.example.json`)."""

    def now(self) -> Timestamp:
        return Timestamp(datetime(2026, 9, 1, 10, 0, tzinfo=TAIPEI_TZ))


def _build(valid_settings_raw: dict[str, Any]) -> ServiceContainer:
    return build_services(
        validate_startup(valid_settings_raw),
        runtime_overrides=RuntimeOverrides(
            clock=_FixedClock(),  # type: ignore[arg-type]
            quote_gateway_factory=FakeYuantaQuoteComHost,
        ),
    )


@pytest.fixture
def test_env(valid_settings_raw: dict[str, Any]) -> Iterator[ServiceContainer]:
    services = _build(valid_settings_raw)
    auto_select_startup_instrument(services)
    start_test_env_broker_session(services)
    start_test_env_quote_login(services, "quote-user", "pw")
    services.event_coordinator.start()
    try:
        yield services
    finally:
        services.event_coordinator.stop(timeout=3)


def _submit_and_fill(
    services: ServiceContainer, *, key: str, wf: str, side: Side, kind: OrderKind, price: str
) -> None:
    current = services.instrument_selection.current
    assert current is not None
    intent = services.order_manager.submit(
        OrderRequest(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=current.contract,
            side=side,
            quantity=Quantity(1),
            price=Price(Decimal("18500")),
            kind=kind,
            time_in_force=TimeInForce.ROD,
            idempotency_key=key,
            workflow_id=wf,
            reason="test-env acceptance",
        )
    )
    gateway = getattr(services.trade_gateway, "_inner", services.trade_gateway)
    gateway.simulate_ack(intent.client_order_id, f"B-{key}")
    gateway.simulate_fill(intent.client_order_id, 1, Decimal(price), broker_fill_no=f"F-{key}")
    # `stop()` drains every queued event; restart to keep going
    services.event_coordinator.stop(timeout=3)
    services.event_coordinator.start()


def _open_and_close(services: ServiceContainer) -> None:
    _submit_and_fill(
        services, key="open", wf="wf-o", side=Side.BUY, kind=OrderKind.OPEN, price="18000"
    )
    _submit_and_fill(
        services, key="close", wf="wf-c", side=Side.SELL, kind=OrderKind.CLOSE, price="18050"
    )


# --- checklist / scenario 12 ----------------------------------------------------------


def test_build_wires_the_local_simulator_and_the_real_quote_factory(
    valid_settings_raw: dict[str, Any],
) -> None:
    services = _build(valid_settings_raw)
    assert services.simulation is True
    assert isinstance(getattr(services.trade_gateway, "_inner"), MockTradeGateway)  # noqa: B009
    assert isinstance(getattr(services.broker_session, "_inner"), MockBrokerSession)  # noqa: B009
    assert services.fill_ledger_service.source == "SIMULATION"


def test_trade_api_never_logged_in(test_env: ServiceContainer) -> None:
    session = getattr(test_env.broker_session, "_inner")  # noqa: B009
    assert isinstance(session, MockBrokerSession)
    assert [r.user_id for r in session.start_calls] == ["TEST-SIMULATION"]


def test_quote_credentials_reach_only_the_quote_host(test_env: ServiceContainer) -> None:
    host = FakeYuantaQuoteComHost.instances[-1]
    assert host.connect_calls == [("quote-user", "pw")]
    session = getattr(test_env.broker_session, "_inner")  # noqa: B009
    assert all(r.user_id == "TEST-SIMULATION" for r in session.start_calls)


def test_all_executions_originate_from_the_simulator(test_env: ServiceContainer) -> None:
    _open_and_close(test_env)
    report = test_env.trade_report_facade.build_report(*_WIDE)
    assert report.fills
    assert all(f.source == "SIMULATION" and f.simulation for f in report.fills)


def test_orders_positions_pnl_report_use_the_production_view_models(
    test_env: ServiceContainer,
) -> None:
    _open_and_close(test_env)
    orders = build_orders_view(test_env.order_repository)
    assert [r.effect for r in orders] == ["開", "平"]
    assert {r.status for r in orders} == {"全部成交"}

    report = test_env.trade_report_facade.build_report(*_WIDE)
    assert len(report.realized_trades) == 1
    assert report.simulation is True
    assert all(t.simulation for t in report.realized_trades)

    csv_text = test_env.trade_report_facade.export_csv(report).decode("utf-8-sig")
    assert "simulation,true" in csv_text

    assert build_pnl_view(test_env.trade_report_facade, *_WIDE).simulation is True
    positions = build_positions_view(test_env.position_valuation_service)
    assert positions.simulation is True
    assert positions.realized_pnl == report.realized_trades[0].net_pnl
    assert positions.rows == ()  # flat after the close


def test_unrealized_pnl_needs_a_real_mark_and_never_synthesises(
    test_env: ServiceContainer,
) -> None:
    _submit_and_fill(
        test_env, key="open", wf="w", side=Side.BUY, kind=OrderKind.OPEN, price="18000"
    )
    snap = test_env.position_valuation_service.snapshot()
    assert snap.open_positions[0].price_quality is PriceQuality.UNAVAILABLE
    assert snap.unrealized_pnl is None
    assert snap.total_pnl is None


def test_drilldown_walks_trade_to_fills_to_intents(test_env: ServiceContainer) -> None:
    _open_and_close(test_env)
    view = build_trade_report_view(test_env.trade_report_facade, *_WIDE)
    result = drill_down(
        view.report.realized_trades[0],
        view.report,
        test_env.order_repository,
        test_env.audit_timeline_reader,
    )
    assert {f.fill_id for f in result.fills} == {"F-open", "F-close"}
    assert {i.workflow_id for i in result.intents} == {"wf-o", "wf-c"}
    assert {i.status.value for i in result.intents} == {"FILLED"}


def test_restart_rebuilds_an_identical_report_no_duplicates(
    test_env: ServiceContainer, valid_settings_raw: dict[str, Any]
) -> None:
    _open_and_close(test_env)
    before = test_env.trade_report_facade.build_report(*_WIDE).realized_trades
    count = test_env.fill_ledger_service._reports._repository.count()  # noqa: SLF001
    test_env.event_coordinator.stop(timeout=3)

    restarted = _build(valid_settings_raw)
    assert restarted.trade_report_facade.build_report(*_WIDE).realized_trades == before
    assert restarted.fill_ledger_service._reports._repository.count() == count  # noqa: SLF001


def test_fail_closed_and_readiness_row(test_env: ServiceContainer) -> None:
    assert_test_env_fail_closed(test_env)
    readiness = dict(compute_readiness(test_env))
    assert readiness["交易 adapter：本機模擬（fail-closed 已驗證）"] is True


def test_fail_closed_rejects_a_non_simulator_broker(test_env: ServiceContainer) -> None:
    from tfx_quant.infrastructure.yuanta.legacy_broker import LegacyBroker

    real = LegacyBroker(event_publisher=None, symbol_resolver=lambda _o: "MXFI6")  # type: ignore[arg-type]
    test_env.trade_gateway = real  # type: ignore[assignment]
    test_env.broker_session = real  # type: ignore[assignment]
    with pytest.raises(TestEnvStartupError):
        assert_test_env_fail_closed(test_env)


def test_start_test_env_helpers_refuse_in_production(
    valid_settings_raw: dict[str, Any],
) -> None:
    prod = build_services(
        validate_startup({**valid_settings_raw, "environment": "PRODUCTION"}),
        runtime_overrides=RuntimeOverrides(quote_gateway_factory=FakeYuantaQuoteComHost),
    )
    with pytest.raises(TestEnvStartupError):
        start_test_env_broker_session(prod)


# --- 執行環境 selector (模擬下單 ↔ 正式下單) ------------------------------------------


def test_environment_switch_allowed_when_idle(test_env: ServiceContainer) -> None:
    assert environment_switch_blocked_reason(test_env) is None


def test_environment_switch_blocked_while_strategy_is_live(test_env: ServiceContainer) -> None:
    test_env.strategy_state_machine.transition(StrategyState.STARTING)
    test_env.strategy_state_machine.transition(StrategyState.RUNNING)
    reason = environment_switch_blocked_reason(test_env)
    assert reason is not None and "策略" in reason


def test_environment_switch_blocked_while_an_order_is_active(test_env: ServiceContainer) -> None:
    current = test_env.instrument_selection.current
    assert current is not None
    test_env.order_manager.submit(
        OrderRequest(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=current.contract,
            side=Side.BUY,
            quantity=Quantity(1),
            price=Price(Decimal("18500")),
            kind=OrderKind.OPEN,
            time_in_force=TimeInForce.ROD,
            idempotency_key="left-open",
            workflow_id="left-open",
            reason="active order",
        )
    )
    reason = environment_switch_blocked_reason(test_env)
    assert reason is not None and "委託" in reason
