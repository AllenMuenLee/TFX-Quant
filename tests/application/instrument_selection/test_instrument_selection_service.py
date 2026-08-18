from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.events.events import Event, InstrumentSwitchCompleted
from tfx_quant.application.instrument_selection.errors import (
    InstrumentMasterEntryNotFoundError,
    SwitchBlockedError,
)
from tfx_quant.application.instrument_selection.instrument_selection_service import (
    InstrumentSelectionService,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.bar_signal_state import InMemoryBarSignalStateStore
from tfx_quant.infrastructure.yuanta.instrument_master_repository import (
    JsonInstrumentMasterRepository,
)
from tfx_quant.infrastructure.yuanta.mock_quote_gateway import MockQuoteGateway
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway

_ACCOUNT = TradingAccount(branch_id="F00", account_no="9808900")
_MXF_CONTRACT = ContractMonth(year=2026, month=9)
_MXF_DEC_CONTRACT = ContractMonth(year=2026, month=12)
_NOW = Timestamp(datetime.fromisoformat("2026-08-18T10:00:00").replace(tzinfo=TAIPEI_TZ))


class FakeClock:
    def now(self) -> Timestamp:
        return _NOW


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


def _write_repo(tmp_path: Path, entries: list[dict[str, object]]) -> JsonInstrumentMasterRepository:
    path = tmp_path / "instrument_master.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return JsonInstrumentMasterRepository(path)


def _entry_dict(
    instrument: str, contract: ContractMonth, *, multiplier: str, expiry: str, tradable: bool = True
) -> dict[str, object]:
    return {
        "instrument": instrument,
        "contract": contract.code,
        "vendor_symbol": f"{instrument}{contract.code}-TEST",
        "broker_product_code": instrument,
        "tick_size": "1",
        "multiplier": multiplier,
        "day_session_start": "08:45",
        "day_session_end": "13:45",
        "night_session_start": None,
        "night_session_end": None,
        "expiry_date": expiry,
        "tradable": tradable,
    }


def _master_repo(tmp_path: Path) -> JsonInstrumentMasterRepository:
    entries = [
        _entry_dict("MXF", _MXF_CONTRACT, multiplier="50", expiry="2026-09-16"),
        _entry_dict("MXF", _MXF_DEC_CONTRACT, multiplier="50", expiry="2026-12-16"),
        _entry_dict("TXF", _MXF_CONTRACT, multiplier="200", expiry="2026-09-16"),
    ]
    return _write_repo(tmp_path, entries)


def _build(
    tmp_path: Path,
    *,
    state: StrategyState = StrategyState.STOPPED,
    positions: tuple[Position, ...] = (),
    open_orders: tuple[Order, ...] = (),
    event_publisher: RecordingEventPublisher | EventCoordinator | None = None,
) -> tuple[
    InstrumentSelectionService, MockTradeGateway, MockQuoteGateway, InMemoryBarSignalStateStore
]:
    machine = StrategyStateMachine()
    if state is StrategyState.PAUSED_SAFE:
        machine.transition(StrategyState.STARTING)
        machine.transition(StrategyState.RUNNING)
        machine.transition(StrategyState.PAUSED_SAFE)
    elif state is not StrategyState.STOPPED:
        raise AssertionError(f"unsupported test state {state}")

    trade_gateway = MockTradeGateway(positions=positions, open_orders=open_orders)
    quote_gateway = MockQuoteGateway()
    bar_signal_state_store = InMemoryBarSignalStateStore()
    service = InstrumentSelectionService(
        strategy_state_machine=machine,
        trade_gateway=trade_gateway,
        quote_gateway=quote_gateway,
        instrument_master=_master_repo(tmp_path),
        bar_signal_state_store=bar_signal_state_store,
        clock=FakeClock(),
        event_publisher=event_publisher,
    )
    return service, trade_gateway, quote_gateway, bar_signal_state_store


def _position(instrument: Instrument, contract: ContractMonth, lots: int) -> Position:
    return Position(
        account=_ACCOUNT,
        instrument=instrument,
        contract=contract,
        net=NetPosition(lots=lots),
        average_price=None if lots == 0 else Price(Decimal("17500")),
        as_of=_NOW,
    )


def _order(instrument: Instrument, contract: ContractMonth) -> Order:
    return Order(
        client_order_id=ClientOrderId(uuid4()),
        account=_ACCOUNT,
        instrument=instrument,
        contract=contract,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("17500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
    )


# -- resolve_manual / resolve_near_month ---------------------------------------------


def test_resolve_manual_returns_matching_entry(tmp_path: Path) -> None:
    service, *_ = _build(tmp_path)
    resolved = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)
    assert resolved.entry.vendor_symbol == "MXF202609-TEST"


def test_resolve_manual_raises_for_missing_entry(tmp_path: Path) -> None:
    service, *_ = _build(tmp_path)
    with pytest.raises(InstrumentMasterEntryNotFoundError):
        service.resolve_manual(Instrument.TXF, _MXF_DEC_CONTRACT)


def test_resolve_manual_raises_for_non_tradable_entry(tmp_path: Path) -> None:
    entries = [
        _entry_dict("MXF", _MXF_CONTRACT, multiplier="50", expiry="2026-09-16", tradable=False)
    ]
    repo = _write_repo(tmp_path, entries)
    service = InstrumentSelectionService(
        strategy_state_machine=StrategyStateMachine(),
        trade_gateway=MockTradeGateway(),
        quote_gateway=MockQuoteGateway(),
        instrument_master=repo,
        bar_signal_state_store=InMemoryBarSignalStateStore(),
        clock=FakeClock(),
    )
    with pytest.raises(InstrumentMasterEntryNotFoundError, match="停止交易"):
        service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)


def test_resolve_near_month_picks_earliest_tradable_contract(tmp_path: Path) -> None:
    service, *_ = _build(tmp_path)
    resolved = service.resolve_near_month(Instrument.MXF)
    assert resolved.contract == _MXF_CONTRACT


def test_resolve_near_month_raises_when_everything_expired(tmp_path: Path) -> None:
    entries = [
        _entry_dict("MXF", _MXF_CONTRACT, multiplier="50", expiry="2026-08-01"),
    ]
    repo = _write_repo(tmp_path, entries)
    service = InstrumentSelectionService(
        strategy_state_machine=StrategyStateMachine(),
        trade_gateway=MockTradeGateway(),
        quote_gateway=MockQuoteGateway(),
        instrument_master=repo,
        bar_signal_state_store=InMemoryBarSignalStateStore(),
        clock=FakeClock(),
    )
    with pytest.raises(InstrumentMasterEntryNotFoundError):
        service.resolve_near_month(Instrument.MXF)


# -- check_switch_allowed / switch_to -------------------------------------------------


def test_switch_allowed_when_flat_and_stopped(tmp_path: Path) -> None:
    service, *_ = _build(tmp_path)
    assert service.check_switch_allowed() is None


def test_switch_blocked_while_running(tmp_path: Path) -> None:
    machine = StrategyStateMachine()
    machine.transition(StrategyState.STARTING)
    machine.transition(StrategyState.RUNNING)
    service = InstrumentSelectionService(
        strategy_state_machine=machine,
        trade_gateway=MockTradeGateway(),
        quote_gateway=MockQuoteGateway(),
        instrument_master=_master_repo(tmp_path),
        bar_signal_state_store=InMemoryBarSignalStateStore(),
        clock=FakeClock(),
    )
    reason = service.check_switch_allowed()
    assert reason is not None
    assert "執行中" in reason


def test_switch_allowed_while_paused_safe(tmp_path: Path) -> None:
    service, *_ = _build(tmp_path, state=StrategyState.PAUSED_SAFE)
    assert service.check_switch_allowed() is None


def test_switch_blocked_with_open_position(tmp_path: Path) -> None:
    position = _position(Instrument.MXF, _MXF_CONTRACT, lots=1)
    service, *_ = _build(tmp_path, positions=(position,))
    reason = service.check_switch_allowed()
    assert reason is not None
    assert "持倉" in reason


def test_switch_allowed_with_only_flat_positions(tmp_path: Path) -> None:
    position = _position(Instrument.MXF, _MXF_CONTRACT, lots=0)
    service, *_ = _build(tmp_path, positions=(position,))
    assert service.check_switch_allowed() is None


def test_switch_blocked_with_open_orders(tmp_path: Path) -> None:
    order = _order(Instrument.MXF, _MXF_CONTRACT)
    service, *_ = _build(tmp_path, open_orders=(order,))
    reason = service.check_switch_allowed()
    assert reason is not None
    assert "委託" in reason


def test_switch_to_subscribes_and_clears_bar_state(tmp_path: Path) -> None:
    service, _trade_gateway, quote_gateway, bar_store = _build(tmp_path)
    resolved = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)

    service.switch_to(resolved)

    assert service.current == resolved
    assert quote_gateway.subscriptions[-1].instrument == Instrument.MXF
    assert quote_gateway.subscriptions[-1].contract == _MXF_CONTRACT
    assert bar_store.clear_calls == [(Instrument.MXF, _MXF_CONTRACT)]


def test_switch_to_unsubscribes_previous_selection(tmp_path: Path) -> None:
    service, _trade_gateway, quote_gateway, _bar_store = _build(tmp_path)
    first = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)
    service.switch_to(first)

    second = service.resolve_manual(Instrument.MXF, _MXF_DEC_CONTRACT)
    service.switch_to(second)

    assert quote_gateway.unsubscriptions[0].instrument == Instrument.MXF
    assert quote_gateway.unsubscriptions[0].contract == _MXF_CONTRACT
    assert service.current == second


def test_switch_to_raises_when_blocked(tmp_path: Path) -> None:
    order = _order(Instrument.MXF, _MXF_CONTRACT)
    service, *_ = _build(tmp_path, open_orders=(order,))
    resolved = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)
    with pytest.raises(SwitchBlockedError):
        service.switch_to(resolved)


def test_switch_to_does_not_mutate_current_when_blocked(tmp_path: Path) -> None:
    service, trade_gateway, quote_gateway, _bar_store = _build(tmp_path)
    first = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)
    service.switch_to(first)

    # Simulate a position appearing for the currently-selected contract before the
    # next switch attempt.
    trade_gateway.add_position(_position(Instrument.MXF, _MXF_CONTRACT, lots=1))

    second = service.resolve_manual(Instrument.MXF, _MXF_DEC_CONTRACT)
    with pytest.raises(SwitchBlockedError):
        service.switch_to(second)

    assert service.current == first


def test_switch_to_publishes_event(tmp_path: Path) -> None:
    publisher = RecordingEventPublisher()
    service, *_ = _build(tmp_path, event_publisher=publisher)
    resolved = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)

    service.switch_to(resolved)

    switch_events = [e for e in publisher.events if isinstance(e, InstrumentSwitchCompleted)]
    assert len(switch_events) == 1
    assert switch_events[0].instrument == Instrument.MXF
    assert switch_events[0].contract == _MXF_CONTRACT


def test_switch_to_works_with_real_event_coordinator(tmp_path: Path) -> None:
    coordinator = EventCoordinator()
    coordinator.start()
    try:
        service, *_ = _build(tmp_path, event_publisher=coordinator)
        resolved = service.resolve_manual(Instrument.MXF, _MXF_CONTRACT)
        service.switch_to(resolved)
    finally:
        coordinator.stop(timeout=1.0)
