from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import Event, LatestPriceObserved
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.trade_reports.fee_model import FillFeeModel
from tfx_quant.application.trade_reports.fill_ledger_service import FillLedgerService
from tfx_quant.application.trade_reports.position_valuation_service import (
    PositionValuationService,
)
from tfx_quant.application.trade_reports.report_facade import TradeReportFacade
from tfx_quant.application.trade_reports.service import TradeReportService
from tfx_quant.desktop.view_models.orders_view_model import build_orders_view
from tfx_quant.desktop.view_models.pnl_view_model import build_pnl_view
from tfx_quant.desktop.view_models.positions_view_model import build_positions_view
from tfx_quant.desktop.view_models.trade_report_view_model import (
    build_trade_report_view,
    drill_down,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository
from tfx_quant.telemetry.audit import AuditTimelineStep

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(2026, 9)
_WIDE = (date(2000, 1, 1), date(2100, 1, 1))


class FakeEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)
        return lambda: None

    def publish(self, event: Event) -> None:
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in handlers:
                    handler(event)


class FakeClock:
    def now(self) -> Timestamp:
        return Timestamp(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ))


def _wire() -> dict[str, Any]:
    bus = FakeEventBus()
    gateway = MockTradeGateway(event_publisher=bus)
    order_repo = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    ledger = SqliteFillLedgerRepository(sqlite3.connect(":memory:", check_same_thread=False))
    report_service = TradeReportService(ledger)
    manager = OrderManager(
        trade_gateway=gateway,
        order_repository=order_repo,
        clock=FakeClock(),
        id_generator=UuidIdGenerator(),
        event_bus=bus,
        position_lookup=lambda _a, _i, _c: NetPosition(0),
    )
    FillLedgerService(
        report_service=report_service,
        order_repository=order_repo,
        trading_day_resolver=lambda ts, _i, _c: ts.value.date(),
        multiplier_lookup=lambda _i, _c: Decimal("50"),
        fee_model=FillFeeModel(
            version="t", commission_per_lot=Decimal("10"), tax_rate=Decimal("0")
        ),
        event_bus=bus,
        simulation=True,
        source="SIMULATION",
    )
    facade = TradeReportFacade(report_service, ledger, lambda _i, _c: Decimal("50"))
    valuation = PositionValuationService(
        fill_ledger=ledger,
        multiplier_lookup=lambda _i, _c: Decimal("50"),
        clock=FakeClock(),
        event_bus=bus,
        simulation=True,
    )
    return {
        "bus": bus,
        "gateway": gateway,
        "order_repo": order_repo,
        "ledger": ledger,
        "facade": facade,
        "valuation": valuation,
        "manager": manager,
    }


def _fill(
    w: dict[str, Any],
    *,
    key: str,
    wf: str,
    side: Side,
    qty: int,
    price: str,
    fill_no: str,
    kind: OrderKind,
) -> Any:
    intent = w["manager"].submit(
        OrderRequest(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            side=side,
            quantity=Quantity(qty),
            price=Price(Decimal("18500")),
            kind=kind,
            time_in_force=TimeInForce.ROD,
            idempotency_key=key,
            workflow_id=wf,
            reason="test",
        )
    )
    w["gateway"].simulate_ack(intent.client_order_id, f"B-{key}")
    w["gateway"].simulate_fill(intent.client_order_id, qty, Decimal(price), broker_fill_no=fill_no)
    return intent


def test_orders_view_rows_and_attention_flag() -> None:
    w = _wire()
    _fill(
        w,
        key="buy",
        wf="wf-1",
        side=Side.BUY,
        qty=2,
        price="18500",
        fill_no="F1",
        kind=OrderKind.OPEN,
    )

    rows = build_orders_view(w["order_repo"])

    assert len(rows) == 1
    assert rows[0].direction == "多"
    assert rows[0].effect == "開"
    assert rows[0].lots == 2
    assert rows[0].cumulative_filled == 2
    assert rows[0].status == "全部成交"
    assert rows[0].needs_attention is False
    assert "*" in (rows[0].broker_order_no or "")


def test_pnl_view_is_marked_simulation() -> None:
    w = _wire()
    _fill(
        w,
        key="buy",
        wf="wf-o",
        side=Side.BUY,
        qty=1,
        price="100",
        fill_no="F1",
        kind=OrderKind.OPEN,
    )
    _fill(
        w,
        key="sell",
        wf="wf-c",
        side=Side.SELL,
        qty=1,
        price="110",
        fill_no="F2",
        kind=OrderKind.CLOSE,
    )

    view = build_pnl_view(w["facade"], *_WIDE)

    assert view.simulation is True
    assert view.daily[0].simulation is True
    assert view.daily[0].net_pnl == Decimal("480")  # 500 gross - 20 commission (2 lots @ 10)


def test_positions_view_shows_quality_and_no_number_when_no_mark() -> None:
    w = _wire()
    _fill(
        w,
        key="buy",
        wf="wf-o",
        side=Side.BUY,
        qty=1,
        price="100",
        fill_no="F1",
        kind=OrderKind.OPEN,
    )

    view = build_positions_view(w["valuation"])

    assert view.rows[0].net_lots == 1
    assert view.rows[0].price_quality == "尚無有效報價"
    assert view.rows[0].unrealized_pnl is None
    assert view.total_pnl is None
    assert view.simulation is True


def test_positions_view_values_from_a_fresh_mark() -> None:
    w = _wire()
    _fill(
        w,
        key="buy",
        wf="wf-o",
        side=Side.BUY,
        qty=1,
        price="100",
        fill_no="F1",
        kind=OrderKind.OPEN,
    )
    w["bus"].publish(
        LatestPriceObserved(
            at=Timestamp(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ)),
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            price=Decimal("130"),
            observed_at=Timestamp(datetime(2026, 8, 25, 11, tzinfo=TAIPEI_TZ)),
            quality="OK",
        )
    )

    view = build_positions_view(w["valuation"])

    assert view.rows[0].price_quality == "即時"
    assert view.rows[0].unrealized_pnl == Decimal("1500")  # (130-100)*50


def test_trade_report_drilldown_walks_fills_intents_and_audit_timeline() -> None:
    w = _wire()
    _fill(
        w,
        key="buy",
        wf="corr-1",
        side=Side.BUY,
        qty=1,
        price="100",
        fill_no="F-open",
        kind=OrderKind.OPEN,
    )
    _fill(
        w,
        key="sell",
        wf="corr-2",
        side=Side.SELL,
        qty=1,
        price="110",
        fill_no="F-close",
        kind=OrderKind.CLOSE,
    )

    view = build_trade_report_view(w["facade"], *_WIDE)
    assert len(view.report.realized_trades) == 1
    trade = view.report.realized_trades[0]

    timeline_by_wf = {
        "corr-1": (AuditTimelineStep(1, "t", "INFO", "s", "order_intent_persist_result", {}),),
        "corr-2": (AuditTimelineStep(2, "t", "INFO", "s", "order_state_transitioned", {}),),
    }
    result = drill_down(trade, view.report, w["order_repo"], lambda wf: timeline_by_wf.get(wf, ()))

    assert {f.fill_id for f in result.fills} == {"F-open", "F-close"}
    assert set(result.order_correlations) == {"corr-1", "corr-2"}
    assert {i.workflow_id for i in result.intents} == {"corr-1", "corr-2"}
    assert [s.seq for s in result.timeline] == [1, 2]
