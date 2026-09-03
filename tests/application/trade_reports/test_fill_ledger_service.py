from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import (
    Event,
    FillReceived,
    TradeLedgerAppendFailed,
    TradeLedgerFillRecorded,
)
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.ports.fill_ledger_repository import FillAppendOutcome
from tfx_quant.application.trade_reports.fee_model import PROVISIONAL_FEE_MODEL, FillFeeModel
from tfx_quant.application.trade_reports.fill_ledger_service import FillLedgerService
from tfx_quant.application.trade_reports.report_facade import TradeReportFacade
from tfx_quant.application.trade_reports.service import TradeReportService
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderIntent
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trade_report import PositionEffect
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount(branch_id="0001", account_no="1234567")
_CONTRACT = ContractMonth(year=2026, month=9)
_PRICE = Price(Decimal("18500"))
_MULT = Decimal("50")
_DAY = date(2026, 8, 25)
_WIDE = (date(2000, 1, 1), date(2100, 1, 1))


class FakeEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        self.published: list[Event] = []

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)
        return lambda: self._handlers[event_type].remove(handler)

    def publish(self, event: Event) -> None:
        self.published.append(event)
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in list(handlers):
                    handler(event)

    def of_type(self, event_type: type[Event]) -> list[Any]:
        return [e for e in self.published if isinstance(e, event_type)]


class FakeClock:
    def now(self) -> Timestamp:
        return Timestamp(datetime(2026, 8, 25, 9, tzinfo=TAIPEI_TZ))


def _trading_day(ts: Timestamp, _i: Instrument, _c: ContractMonth) -> date:
    return ts.value.date()


def _multiplier(_i: Instrument, _c: ContractMonth) -> Decimal:
    return _MULT


@dataclass
class Wiring:
    manager: OrderManager
    gateway: MockTradeGateway
    ledger: SqliteFillLedgerRepository
    facade: TradeReportFacade
    bus: FakeEventBus


def _wire(*, fee_model: FillFeeModel = PROVISIONAL_FEE_MODEL, simulation: bool = True) -> Wiring:
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
    # After OrderManager on purpose: its FillReceived subscription must run second so the
    # intent it reads is already post-ACK and post-apply_fill.
    FillLedgerService(
        report_service=report_service,
        order_repository=order_repo,
        trading_day_resolver=_trading_day,
        multiplier_lookup=_multiplier,
        fee_model=fee_model,
        event_bus=bus,
        simulation=simulation,
        source="SIMULATION" if simulation else "YUANTA_OCX",
    )
    facade = TradeReportFacade(report_service, ledger, _multiplier)
    return Wiring(manager, gateway, ledger, facade, bus)


def _request(
    *, key: str, wf: str, side: Side = Side.BUY, qty: int = 1, kind: OrderKind = OrderKind.OPEN
) -> OrderRequest:
    return OrderRequest(
        account=_ACCOUNT,
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=side,
        quantity=Quantity(qty),
        price=_PRICE,
        kind=kind,
        time_in_force=TimeInForce.ROD,
        idempotency_key=key,
        workflow_id=wf,
        reason="test",
    )


def _submit_filled(w: Wiring, req: OrderRequest, *, price: str, fill_no: str) -> OrderIntent:
    intent = w.manager.submit(req)
    w.gateway.simulate_ack(intent.client_order_id, f"B-{req.idempotency_key}")
    w.gateway.simulate_fill(
        intent.client_order_id, req.quantity.lots, Decimal(price), broker_fill_no=fill_no
    )
    return intent


def test_applied_fill_lands_in_the_ledger_with_intent_context() -> None:
    w = _wire()
    _submit_filled(w, _request(key="buy", wf="workflow-42"), price="18500", fill_no="F-1")

    recorded = w.bus.of_type(TradeLedgerFillRecorded)
    assert len(recorded) == 1
    assert recorded[0].fill_id == "F-1"
    assert recorded[0].outcome == FillAppendOutcome.INSERTED.value
    assert recorded[0].order_correlation == "workflow-42"
    assert recorded[0].simulation is True

    ledger = list(w.ledger.list_between(*_WIDE))
    assert len(ledger) == 1
    lf = ledger[0]
    assert lf.fill_id == "F-1"
    assert lf.broker_order_no == "B-buy"
    assert lf.order_correlation == "workflow-42"
    assert lf.masked_account == "***4567"
    assert lf.position_effect is PositionEffect.OPEN
    assert lf.quantity == 1
    assert lf.price == Decimal("18500")
    assert lf.simulation is True
    assert lf.source == "SIMULATION"


def test_duplicate_fill_received_is_a_no_op_in_the_ledger() -> None:
    w = _wire()
    _submit_filled(w, _request(key="buy", wf="wf-1"), price="18500", fill_no="F-1")

    w.gateway.replay_last_fill()  # exact same broker fill, re-published

    outcomes = [r.outcome for r in w.bus.of_type(TradeLedgerFillRecorded)]
    assert outcomes == [FillAppendOutcome.INSERTED.value, FillAppendOutcome.DUPLICATE.value]
    assert w.ledger.count() == 1
    report = w.facade.build_report(*_WIDE)
    assert report.realized_trades == ()
    assert len(report.fills) == 1


def test_fill_with_no_matching_intent_fails_closed_without_appending() -> None:
    w = _wire()
    orphan = Fill(
        client_order_id=ClientOrderId(),
        instrument=Instrument.MXF,
        side=Side.BUY,
        quantity=Quantity(1),
        price=_PRICE,
        at=Timestamp(datetime(2026, 8, 25, 10, tzinfo=TAIPEI_TZ)),
        broker_fill_no="F-orphan",
        broker_seq_no=1,
    )

    w.bus.publish(FillReceived(at=orphan.at, fill=orphan))

    assert w.bus.of_type(TradeLedgerFillRecorded) == []
    failed = w.bus.of_type(TradeLedgerAppendFailed)
    assert len(failed) == 1
    assert "no matching order intent" in failed[0].reason
    assert w.ledger.count() == 0


def test_open_then_close_produces_a_realized_trade_via_the_facade() -> None:
    w = _wire(
        fee_model=FillFeeModel(
            version="fees-1", commission_per_lot=Decimal("20"), tax_rate=Decimal("0")
        )
    )
    _submit_filled(
        w, _request(key="buy", wf="wf-open", side=Side.BUY, qty=2), price="100", fill_no="F-open"
    )
    _submit_filled(
        w,
        _request(key="sell", wf="wf-close", side=Side.SELL, qty=2, kind=OrderKind.CLOSE),
        price="110",
        fill_no="F-close",
    )

    report = w.facade.build_report(*_WIDE)
    assert len(report.realized_trades) == 1
    trade = report.realized_trades[0]
    assert trade.gross_pnl == Decimal("1000")  # (110-100) * 50 * 2
    assert all(f.simulation is True for f in report.fills)


def test_provisional_when_fee_model_is_unknown() -> None:
    w = _wire()  # PROVISIONAL_FEE_MODEL
    _submit_filled(w, _request(key="buy", wf="wf-1"), price="18500", fill_no="F-1")

    lf = list(w.ledger.list_between(*_WIDE))[0]
    assert lf.commission is None
    assert lf.tax is None
    assert lf.provisional_reasons == ("commission_unknown", "tax_unknown")


def test_subscriber_order_ledger_always_sees_post_apply_fill_intent() -> None:
    """OrderManager and FillLedgerService share one bus; the ledger's FillReceived
    handler must run after OrderManager's so the joined intent already has its
    broker_order_no and FILLED status."""
    w = _wire()
    intent = w.manager.submit(_request(key="buy", wf="wf-1"))
    w.gateway.simulate_ack(intent.client_order_id, "B-buy")
    w.gateway.simulate_fill(intent.client_order_id, 1, Decimal("18500"), broker_fill_no="F-1")

    lf = list(w.ledger.list_between(*_WIDE))[0]
    assert lf.broker_order_no == "B-buy"  # would be missing if the ledger ran first
