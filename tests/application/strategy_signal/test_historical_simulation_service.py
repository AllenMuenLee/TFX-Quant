from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import Event
from tfx_quant.application.order_management.order_manager import OrderManager, OrderRequest
from tfx_quant.application.strategy_signal.historical_simulation_service import (
    HistoricalSimulationService,
)
from tfx_quant.application.strategy_signal.history_replay import (
    HistoricalReplay,
    replay_history_state,
)
from tfx_quant.desktop.view_models.historical_trades_view_model import missed_trades
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord, MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import OrderKind, TimeInForce
from tfx_quant.domain.order_state_machine import OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind
from tfx_quant.domain.strategy_signal_engine import EngineConfig, PositionSide
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.infrastructure.identity import UuidIdGenerator
from tfx_quant.infrastructure.yuanta.mock_trade_gateway import MockTradeGateway
from tfx_quant.persistence.sqlite_order_repository import SqliteOrderRepository

_ACCOUNT = TradingAccount("SIM", "0001")
_CONTRACT = ContractMonth(2026, 9)


class _Bus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)
        return lambda: self._handlers[event_type].remove(handler)

    def publish(self, event: Event) -> None:
        for event_type, handlers in tuple(self._handlers.items()):
            if isinstance(event, event_type):
                for handler in tuple(handlers):
                    handler(event)


class _Clock:
    def now(self) -> Timestamp:
        return Timestamp(datetime(2026, 9, 1, 14, 0, tzinfo=TAIPEI_TZ))


def _record(index: int, close: str) -> BarRecord:
    start = Timestamp(datetime(2026, 9, 1, 10 + index, 0, tzinfo=TAIPEI_TZ))
    end = Timestamp(start.value + timedelta(hours=1))
    value = Decimal(close)
    bar = Bar(
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        open=Price(value - 5),
        high=Price(value),
        low=Price(value - 5),
        close=Price(value),
        volume=10,
        start=start,
        end=end,
    )
    return BarRecord(
        bar=bar,
        period=BarPeriod.SIXTY_MINUTE,
        trading_day=date(2026, 9, 1),
        session=MarketSession.DAY,
        source=BarDataSource.MANUAL,
        is_gap_recovery=False,
        created_at=end,
        updated_at=end,
    )


def test_history_signals_fill_in_order_update_inventory_and_enable_add_on() -> None:
    records = tuple(
        _record(index, close)
        for index, close in enumerate(("100", "110", "120", "130", "140"))
    )
    replay = replay_history_state(
        records,
        lambda end: Timestamp(end.value + timedelta(hours=1)),
        EngineConfig(ma_window=2, flat_lookback=2, flat_threshold_points=Decimal("1")),
    )
    assert [trade.decision.signal_kind for trade in replay.trades] == [
        SignalKind.ENTER_LONG,
        SignalKind.ADD_LONG,
    ]

    bus = _Bus()
    gateway = MockTradeGateway(
        event_publisher=bus, track_positions_from_fills=True, execute_on_submit=True
    )
    repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))

    def position_lookup(
        account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> NetPosition:
        return next(
            (
                position.net
                for position in gateway.query_positions()
                if position.account == account
                and position.instrument is instrument
                and position.contract == contract
            ),
            NetPosition(0),
        )

    manager = OrderManager(
        trade_gateway=gateway,
        order_repository=repository,
        clock=_Clock(),
        id_generator=UuidIdGenerator(),
        event_bus=bus,
        position_lookup=position_lookup,
    )
    installed = []
    service = HistoricalSimulationService(
        order_manager=manager,
        order_repository=repository,
        gateway=gateway,
        event_bus=bus,
        selected_account=lambda: _ACCOUNT,
        replay_installed=installed.append,
    )

    service.sync(replay)

    assert len(gateway.submitted_orders) == 2
    assert all(order.status.value == "FILLED" for order in repository.list_all())
    positions = gateway.query_positions()
    assert len(positions) == 1
    assert positions[0].net == NetPosition(2)
    assert positions[0].average_price == Price(Decimal("135"))
    assert installed == [replay]
    assert replay.engine is not None
    assert replay.engine.position_side is PositionSide.LONG
    assert len(replay.engine.lots) == 2

    service.sync(replay)
    assert len(gateway.submitted_orders) == 2


def _replay() -> HistoricalReplay:
    records = tuple(
        _record(index, close)
        for index, close in enumerate(("100", "110", "120", "130", "140"))
    )
    return replay_history_state(
        records,
        lambda end: Timestamp(end.value + timedelta(hours=1)),
        EngineConfig(ma_window=2, flat_lookback=2, flat_threshold_points=Decimal("1")),
    )


def _simulation(
    repository: SqliteOrderRepository, gateway: MockTradeGateway
) -> tuple[_Bus, OrderManager, HistoricalSimulationService]:
    bus = _Bus()
    gateway._event_publisher = bus  # noqa: SLF001 - one bus per simulated run

    def position_lookup(
        account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> NetPosition:
        return next(
            (
                position.net
                for position in gateway.query_positions()
                if position.account == account and position.contract == contract
            ),
            NetPosition(0),
        )

    manager = OrderManager(
        trade_gateway=gateway,
        order_repository=repository,
        clock=_Clock(),
        id_generator=UuidIdGenerator(),
        event_bus=bus,
        position_lookup=position_lookup,
    )
    service = HistoricalSimulationService(
        order_manager=manager,
        order_repository=repository,
        gateway=gateway,
        event_bus=bus,
        selected_account=lambda: _ACCOUNT,
        replay_installed=lambda _replay: None,
    )
    return bus, manager, service


def _earlier_run_submit(repository: SqliteOrderRepository, key: str, price: str) -> None:
    """An earlier run whose simulator never filled what it was sent."""
    _, manager, _ = _simulation(repository, MockTradeGateway())
    manager.submit(
        OrderRequest(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            side=Side.BUY,
            quantity=Quantity(1),
            price=Price(Decimal(price)),
            kind=OrderKind.OPEN,
            time_in_force=TimeInForce.ROD,
            idempotency_key=key,
            workflow_id=key,
            reason="earlier run",
        )
    )


def test_order_left_open_by_an_earlier_run_is_executed_at_the_replay_price() -> None:
    replay = _replay()
    repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    entry_key = replay.trades[0].decision.intent_key
    assert entry_key is not None
    _earlier_run_submit(repository, entry_key, "999")

    gateway = MockTradeGateway(track_positions_from_fills=True, execute_on_submit=True)
    _, _, service = _simulation(repository, gateway)
    service.sync(replay)

    orders = repository.list_all()
    assert len(orders) == 2
    assert all(order.status is OrderStatus.FILLED for order in orders)
    entry = repository.find_by_idempotency_key(entry_key)
    assert entry is not None
    replay_price = replay.trades[0].decision.current_price
    assert replay_price is not None and entry.avg_fill_price == Price(replay_price)
    assert gateway.query_positions()[0].net == NetPosition(2)


def test_order_left_open_that_the_replay_no_longer_wants_is_cancelled() -> None:
    repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    _earlier_run_submit(repository, "stale-live-decision", "120")

    gateway = MockTradeGateway(track_positions_from_fills=True, execute_on_submit=True)
    _, _, service = _simulation(repository, gateway)
    service.sync(_replay())

    stale = repository.find_by_idempotency_key("stale-live-decision")
    assert stale is not None and stale.status is OrderStatus.CANCELLED
    assert gateway.query_positions()[0].net == NetPosition(2)


def test_restart_restores_filled_inventory_and_only_executes_what_is_missing() -> None:
    replay = _replay()
    repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    first = MockTradeGateway(track_positions_from_fills=True, execute_on_submit=True)
    _, _, service = _simulation(repository, first)
    service.sync(replace(replay, trades=replay.trades[:1]))
    assert first.query_positions()[0].net == NetPosition(1)

    restarted = MockTradeGateway(track_positions_from_fills=True, execute_on_submit=True)
    restarted.restore_filled(repository.list_all())
    assert restarted.query_positions()[0].net == NetPosition(1)
    _, _, service = _simulation(repository, restarted)
    service.sync(replay)

    assert len(restarted.submitted_orders) == 1
    assert restarted.query_positions()[0].net == NetPosition(2)


def test_position_already_at_the_replay_target_is_not_traded_again() -> None:
    replay = _replay()
    repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    gateway = MockTradeGateway(track_positions_from_fills=True, execute_on_submit=True)
    gateway.add_position(
        Position(
            account=_ACCOUNT,
            instrument=Instrument.MXF,
            contract=_CONTRACT,
            net=NetPosition(1),
            average_price=Price(Decimal("130")),
            as_of=replay.trades[0].decision.at,
        )
    )
    _, _, service = _simulation(repository, gateway)
    service.sync(replay)

    # The entry already happened another way; only the add-on is executed.
    assert [order.quantity.lots for order in gateway.submitted_orders] == [1]
    assert gateway.query_positions()[0].net == NetPosition(2)


def test_order_left_open_for_a_later_candidate_is_cancelled_and_that_candidate_retried() -> None:
    replay = _replay()
    repository = SqliteOrderRepository(sqlite3.connect(":memory:", check_same_thread=False))
    add_key = replay.trades[1].decision.intent_key
    assert add_key is not None
    _earlier_run_submit(repository, add_key, "999")

    gateway = MockTradeGateway(track_positions_from_fills=True, execute_on_submit=True)
    _, _, service = _simulation(repository, gateway)
    service.sync(replay)

    stale = repository.find_by_idempotency_key(add_key)
    assert stale is not None and stale.status is OrderStatus.CANCELLED
    retried = repository.find_by_idempotency_key(f"{add_key}#retry1")
    assert retried is not None and retried.status is OrderStatus.FILLED
    assert gateway.query_positions()[0].net == NetPosition(2)
    assert missed_trades(replay.trades, repository.list_all(), _ACCOUNT) == []
