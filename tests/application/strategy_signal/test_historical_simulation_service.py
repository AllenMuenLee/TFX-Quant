from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from tfx_quant.application.events.events import Event
from tfx_quant.application.order_management.order_manager import OrderManager
from tfx_quant.application.strategy_signal.historical_simulation_service import (
    HistoricalSimulationService,
)
from tfx_quant.application.strategy_signal.history_replay import replay_history_state
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord, MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.quantity import NetPosition
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
    gateway = MockTradeGateway(event_publisher=bus, track_positions_from_fills=True)
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
