from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from tfx_quant.application.connectivity.connectivity_monitor import ConnectivityMonitor
from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerSessionInvalidated,
    ChannelHealthChanged,
    ConnectivityReconciled,
    Event,
    FillReceived,
    MarketDataFreshnessChanged,
    MarketDataTickReceived,
    OrderReportReceived,
    SafePauseTriggered,
)
from tfx_quant.application.ports.broker_session import (
    LoginRequest,
    LogoutReason,
    SessionCapabilities,
)
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.connectivity import ChannelId, SafePauseReason
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.mock_broker_session import MockBrokerSession

_CONTRACT = ContractMonth(year=2026, month=9)


class FakeEventBus:
    """Synchronous, in-process event bus — same shape as `tests/application/
    order_management/test_order_manager.py`'s `FakeEventBus`."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        self.published: list[Event] = []

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]:
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            self._handlers[event_type].remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        self.published.append(event)
        for event_type, handlers in self._handlers.items():
            if isinstance(event, event_type):
                for handler in list(handlers):
                    handler(event)


class FakeClock:
    def __init__(self, now: Timestamp) -> None:
        self._now = now

    def now(self) -> Timestamp:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = Timestamp(self._now.value + timedelta(seconds=seconds))


class _FakeCancellable:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class _Scheduled:
    delay_seconds: float
    callback: Callable[[], None]
    token: _FakeCancellable


class FakeScheduler:
    """Captures every scheduled reconnect attempt instead of really sleeping — tests
    fire them explicitly via `fire_latest()`."""

    def __init__(self) -> None:
        self.scheduled: list[_Scheduled] = []

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _FakeCancellable:
        token = _FakeCancellable()
        self.scheduled.append(_Scheduled(delay_seconds, callback, token))
        return token

    def fire_latest(self) -> None:
        entry = self.scheduled[-1]
        if not entry.token.cancelled:
            entry.callback()


def _running_state_machine() -> StrategyStateMachine:
    machine = StrategyStateMachine()
    machine.transition(StrategyState.STARTING)
    machine.transition(StrategyState.RUNNING)
    return machine


def _login_request() -> LoginRequest:
    return LoginRequest(
        environment=Environment.TEST, user_id="F00000000012345678", password=SecretStr("x")
    )


_SetupResult = tuple[
    ConnectivityMonitor,
    MockBrokerSession,
    FakeEventBus,
    FakeClock,
    FakeScheduler,
    StrategyStateMachine,
]


def _setup(
    *,
    order_count: int = 0,
    expected_net: NetPosition | None = None,
    max_clock_skew_seconds: float = 5.0,
) -> _SetupResult:
    event_bus = FakeEventBus()
    session = MockBrokerSession(event_publisher=event_bus)
    clock = FakeClock(Timestamp.now())
    scheduler = FakeScheduler()
    state_machine = _running_state_machine()
    monitor = ConnectivityMonitor(
        broker_session=session,
        strategy_state_machine=state_machine,
        clock=clock,
        event_bus=event_bus,
        order_summary_provider=lambda: order_count,
        position_summary_provider=lambda: expected_net,
        max_clock_skew_seconds=max_clock_skew_seconds,
        scheduler=scheduler,
        random_fn=lambda: 0.5,
    )
    return monitor, session, event_bus, clock, scheduler, state_machine


# -- Market data alone going stale -------------------------------------------------------


def test_market_data_alone_going_stale_triggers_pause() -> None:
    monitor, _session, event_bus, _clock, _scheduler, state_machine = _setup()

    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )

    assert state_machine.state is StrategyState.PAUSED_SAFE
    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.MARKET_DATA_STALE
    assert record.channel is ChannelId.MARKET_DATA
    assert monitor.channel_health(ChannelId.MARKET_DATA).connected is False
    assert monitor.channel_health(ChannelId.MARKET_DATA).is_stale is True


def test_market_data_recovering_does_not_untrigger_an_existing_pause() -> None:
    monitor, _session, event_bus, _clock, _scheduler, state_machine = _setup()
    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )
    assert state_machine.state is StrategyState.PAUSED_SAFE

    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=False
        )
    )

    assert monitor.channel_health(ChannelId.MARKET_DATA).connected is True
    # Still paused — nothing auto-resumes.
    assert state_machine.state is StrategyState.PAUSED_SAFE


# -- Trade channel alone invalidated (capability regression, no session invalidation) ----


def test_trade_channel_alone_invalidated_triggers_pause() -> None:
    monitor, _session, event_bus, _clock, _scheduler, state_machine = _setup()
    event_bus.publish(
        BrokerCapabilitiesChanged(
            at=Timestamp.now(),
            capabilities=SessionCapabilities(
                login=True, market_data=True, trading=True, order_reports=True, queries=True
            ),
        )
    )
    assert state_machine.state is StrategyState.RUNNING  # still healthy, nothing regressed yet

    event_bus.publish(
        BrokerCapabilitiesChanged(
            at=Timestamp.now(),
            capabilities=SessionCapabilities(
                login=True, market_data=True, trading=False, order_reports=True, queries=True
            ),
        )
    )

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.TRADE_CHANNEL_INVALID
    assert record.channel is ChannelId.TRADE
    # Order-reports channel is untouched — the two are tracked independently.
    assert monitor.channel_health(ChannelId.ORDER_REPORTS).connected is True


def test_order_reports_alone_interrupted_triggers_its_own_reason() -> None:
    monitor, _session, event_bus, _clock, _scheduler, _state_machine = _setup()
    event_bus.publish(
        BrokerCapabilitiesChanged(
            at=Timestamp.now(),
            capabilities=SessionCapabilities(
                login=True, market_data=True, trading=True, order_reports=True, queries=True
            ),
        )
    )
    event_bus.publish(
        BrokerCapabilitiesChanged(
            at=Timestamp.now(),
            capabilities=SessionCapabilities(
                login=True, market_data=True, trading=True, order_reports=False, queries=True
            ),
        )
    )

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.ORDER_REPORTS_INTERRUPTED
    assert monitor.channel_health(ChannelId.TRADE).connected is True


# -- Full disconnect -----------------------------------------------------------------------


def test_full_disconnect_triggers_pause_and_begins_reconnect() -> None:
    monitor, _session, event_bus, _clock, scheduler, state_machine = _setup()
    monitor.remember_login_request(_login_request())

    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="模擬全部斷線"))

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.TRADE_CHANNEL_INVALID
    assert state_machine.state is StrategyState.PAUSED_SAFE
    for channel in (ChannelId.LOGIN, ChannelId.TRADE, ChannelId.ORDER_REPORTS, ChannelId.QUERIES):
        assert monitor.channel_health(channel).connected is False
    assert monitor.is_reconnecting is True
    assert len(scheduler.scheduled) == 1


def test_reconnect_unavailable_without_a_remembered_login_request() -> None:
    monitor, _session, event_bus, _clock, scheduler, _state_machine = _setup()

    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))

    assert monitor.is_reconnecting is False
    assert scheduler.scheduled == []


# -- Reconnect retry / success / exhaustion / cancellation --------------------------------


def test_reconnect_retries_after_a_failed_attempt_then_succeeds() -> None:
    """Success detection (`_on_session_ready_core`) is subscribed unconditionally at
    construction time — it must work even without
    `attach_reconnect_reconciliation_watcher()` ever being called."""
    monitor, session, event_bus, _clock, scheduler, _state_machine = _setup()
    monitor.remember_login_request(_login_request())
    session.script_start_failures(1, reason="暫時失敗", retriable=True)

    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    assert monitor.is_reconnecting is True
    assert monitor.reconnect_attempt_count == 1

    scheduler.fire_latest()  # attempt #1 -> scripted failure
    assert monitor.is_reconnecting is True
    assert monitor.reconnect_attempt_count == 2
    assert len(scheduler.scheduled) == 2

    scheduler.fire_latest()  # attempt #2 -> succeeds (mock happy path)
    assert monitor.is_reconnecting is False
    for channel in ChannelId:
        assert monitor.channel_health(channel).connected is True
    assert len(session.start_calls) == 2


def test_reconnect_exhausts_after_max_attempts_and_stops() -> None:
    monitor, session, event_bus, _clock, scheduler, _state_machine = _setup()
    monitor.remember_login_request(_login_request())
    session.script_start_failures(10, reason="持續失敗", retriable=True)

    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    max_attempts = 8  # ReconnectBackoffPolicy's own default
    for _ in range(max_attempts - 1):
        assert monitor.is_reconnecting is True
        scheduler.fire_latest()

    assert monitor.is_reconnecting is False
    assert monitor.reconnect_attempt_count == max_attempts


def test_cancel_reconnect_stops_retrying() -> None:
    monitor, _session, event_bus, _clock, scheduler, _state_machine = _setup()
    monitor.remember_login_request(_login_request())

    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    assert monitor.is_reconnecting is True

    monitor.cancel_reconnect()

    assert monitor.is_reconnecting is False
    assert scheduler.scheduled[-1].token.cancelled is True


def test_broker_logged_out_cancels_an_in_progress_reconnect() -> None:
    """`BrokerLoggedOut` is always user-initiated — see
    `application.events.events.BrokerLoggedOut` — so it must never be followed by an
    automatic reconnect."""
    monitor, _session, event_bus, _clock, scheduler, _state_machine = _setup()
    monitor.remember_login_request(_login_request())
    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    assert monitor.is_reconnecting is True

    event_bus.publish(BrokerLoggedOut(at=Timestamp.now(), reason=LogoutReason.USER_REQUESTED))

    assert monitor.is_reconnecting is False
    assert scheduler.scheduled[-1].token.cancelled is True


def test_reconnect_attempt_call_raising_is_treated_as_a_failure_not_a_crash() -> None:
    class _RaisingSession(MockBrokerSession):
        def start(self, request: LoginRequest) -> None:
            raise RuntimeError("boom")

    event_bus = FakeEventBus()
    session = _RaisingSession(event_publisher=event_bus)
    clock = FakeClock(Timestamp.now())
    scheduler = FakeScheduler()
    state_machine = _running_state_machine()
    monitor = ConnectivityMonitor(
        broker_session=session,
        strategy_state_machine=state_machine,
        clock=clock,
        event_bus=event_bus,
        scheduler=scheduler,
    )
    monitor.remember_login_request(_login_request())

    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    assert monitor.is_reconnecting is True
    initial_scheduled = len(scheduler.scheduled)

    scheduler.fire_latest()  # start() raises -> must reschedule, not propagate

    assert monitor.is_reconnecting is True
    assert len(scheduler.scheduled) == initial_scheduled + 1


# -- Heartbeat: ticks alone never change connectivity/pause state (假陽性防護) -----------


def test_heartbeat_tick_alone_never_changes_health_or_pause_state() -> None:
    monitor, _session, event_bus, clock, _scheduler, state_machine = _setup()
    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )
    assert state_machine.state is StrategyState.PAUSED_SAFE
    record_before = monitor.current_pause()
    health_before = monitor.channel_health(ChannelId.MARKET_DATA)

    clock.advance(30.0)
    monitor.on_clock_tick()
    monitor.on_clock_tick()

    assert monitor.current_pause() == record_before
    health_after = monitor.channel_health(ChannelId.MARKET_DATA)
    assert health_after.connected == health_before.connected
    assert health_after.is_stale == health_before.is_stale
    assert health_after.last_error == health_before.last_error
    # The heartbeat stamp itself does move — it just carries no other meaning.
    assert health_after.last_heartbeat_at is not None
    assert health_after.last_heartbeat_at != health_before.last_heartbeat_at


def test_heartbeat_tick_publishes_no_channel_health_changed_event() -> None:
    monitor, _session, event_bus, clock, _scheduler, _state_machine = _setup()
    clock.advance(5.0)
    monitor.on_clock_tick()

    assert not any(isinstance(e, ChannelHealthChanged) for e in event_bus.published)


# -- Query failure / clock skew via the QueryObserver seam --------------------------------


def test_record_query_result_failure_triggers_query_failed_pause() -> None:
    monitor, _session, _event_bus, _clock, _scheduler, state_machine = _setup()

    monitor.record_query_result(
        call="query_positions", ok=False, latency_ms=12.0, error="連線逾時"
    )

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.QUERY_FAILED
    assert record.channel is ChannelId.QUERIES
    assert state_machine.state is StrategyState.PAUSED_SAFE


def test_record_query_result_success_does_not_pause_and_updates_latency() -> None:
    monitor, _session, _event_bus, _clock, _scheduler, state_machine = _setup()

    monitor.record_query_result(call="query_positions", ok=True, latency_ms=42.0, error=None)

    assert monitor.current_pause() is None
    assert state_machine.state is StrategyState.RUNNING
    assert monitor.channel_health(ChannelId.QUERIES).latency_ms == 42.0
    assert monitor.channel_health(ChannelId.QUERIES).connected is True


def test_clock_skew_from_a_queried_position_snapshot_triggers_pause() -> None:
    monitor, _session, _event_bus, clock, _scheduler, state_machine = _setup()
    stale_position = Position(
        account=None,  # type: ignore[arg-type]
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        net=NetPosition(1),
        average_price=Price(Decimal("18500")),
        as_of=Timestamp(clock.now().value - timedelta(seconds=30)),
    )

    monitor.record_query_result(
        call="query_positions", ok=True, latency_ms=5.0, error=None, positions=[stale_position]
    )

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.CLOCK_SKEW


def test_order_report_within_clock_skew_tolerance_does_not_pause() -> None:
    monitor, _session, event_bus, clock, _scheduler, state_machine = _setup()
    report = OrderReport(
        client_order_id=ClientOrderId(),
        status=OrderStatus.ACKNOWLEDGED,
        broker_seq_no=1,
        at=Timestamp(clock.now().value - timedelta(seconds=2)),
    )

    event_bus.publish(OrderReportReceived(at=Timestamp.now(), report=report))

    assert monitor.current_pause() is None
    assert state_machine.state is StrategyState.RUNNING
    assert monitor.channel_health(ChannelId.ORDER_REPORTS).connected is True


def test_order_report_beyond_clock_skew_tolerance_triggers_pause() -> None:
    monitor, _session, event_bus, clock, _scheduler, state_machine = _setup()
    report = OrderReport(
        client_order_id=ClientOrderId(),
        status=OrderStatus.ACKNOWLEDGED,
        broker_seq_no=1,
        at=Timestamp(clock.now().value - timedelta(seconds=45)),
    )

    event_bus.publish(OrderReportReceived(at=Timestamp.now(), report=report))

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.CLOCK_SKEW
    assert record.channel is ChannelId.ORDER_REPORTS


def test_fill_beyond_clock_skew_tolerance_triggers_pause() -> None:
    monitor, _session, event_bus, clock, _scheduler, state_machine = _setup()
    fill = Fill(
        client_order_id=ClientOrderId(),
        instrument=Instrument.MXF,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("18500")),
        at=Timestamp(clock.now().value - timedelta(seconds=45)),
        broker_fill_no="F-1",
        broker_seq_no=1,
    )

    event_bus.publish(FillReceived(at=Timestamp.now(), fill=fill))

    record = monitor.current_pause()
    assert record is not None
    assert record.reason is SafePauseReason.CLOCK_SKEW


# -- First-trigger-wins: never overwrite an existing episode's reason ---------------------


def test_only_the_first_pause_reason_is_recorded_until_resumed() -> None:
    monitor, _session, event_bus, _clock, _scheduler, state_machine = _setup()
    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )
    first = monitor.current_pause()
    assert first is not None
    assert first.reason is SafePauseReason.MARKET_DATA_STALE

    monitor.record_query_result(call="query_positions", ok=False, latency_ms=1.0, error="boom")

    # PAUSED_SAFE -> FAULTED is a legal transition, but a second still-unresolved
    # trigger must never escalate it — same "only escalate from RUNNING" gate as
    # docs/adr/0010-position-reconciliation-and-manual-sync.md decision 2.
    second = monitor.current_pause()
    assert second is not None
    assert second.reason is SafePauseReason.MARKET_DATA_STALE
    assert second.correlation_id == first.correlation_id
    assert state_machine.state is StrategyState.PAUSED_SAFE


def test_pause_is_a_no_op_before_the_strategy_has_ever_run() -> None:
    event_bus = FakeEventBus()
    session = MockBrokerSession(event_publisher=event_bus)
    clock = FakeClock(Timestamp.now())
    state_machine = StrategyStateMachine()  # still STOPPED
    monitor = ConnectivityMonitor(
        broker_session=session,
        strategy_state_machine=state_machine,
        clock=clock,
        event_bus=event_bus,
    )

    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )

    assert monitor.current_pause() is None
    assert state_machine.state is StrategyState.STOPPED


# -- Audit record fields: order/position summary + blocked-intent count -------------------


def test_safe_pause_record_carries_order_and_position_summary() -> None:
    monitor, _session, event_bus, _clock, _scheduler, _state_machine = _setup(
        order_count=3, expected_net=NetPosition(-2)
    )
    monitor.note_blocked_intent("would have opened a new position")

    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )

    record = monitor.current_pause()
    assert record is not None
    assert record.active_order_count == 3
    assert record.expected_net_lots == -2
    assert record.blocked_intent_count == 1
    assert record.strategy_state_before is StrategyState.RUNNING
    assert record.resulting_strategy_state is StrategyState.PAUSED_SAFE


def test_safe_pause_triggered_event_is_published() -> None:
    monitor, _session, event_bus, _clock, _scheduler, _state_machine = _setup()

    event_bus.publish(
        MarketDataFreshnessChanged(
            at=Timestamp.now(), instrument=Instrument.MXF, contract=_CONTRACT, is_stale=True
        )
    )

    triggered = [e for e in event_bus.published if isinstance(e, SafePauseTriggered)]
    assert len(triggered) == 1
    assert triggered[0].record == monitor.current_pause()


# -- ConnectivityReconciled: only after a fresh BrokerSessionReady following a pause ------


def test_connectivity_reconciled_published_on_session_ready_after_a_pause() -> None:
    monitor, session, event_bus, _clock, _scheduler, _state_machine = _setup()
    monitor.attach_reconnect_reconciliation_watcher()
    monitor.remember_login_request(_login_request())
    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    assert monitor.current_pause() is not None
    assert monitor.current_pause().reconciled is False  # type: ignore[union-attr]

    session.simulate_login_success()

    reconciled_events = [e for e in event_bus.published if isinstance(e, ConnectivityReconciled)]
    assert len(reconciled_events) == 1
    assert monitor.current_pause().reconciled is True  # type: ignore[union-attr]


def test_session_ready_before_any_pause_publishes_no_reconciled_event() -> None:
    monitor, session, event_bus, _clock, _scheduler, _state_machine = _setup()
    monitor.attach_reconnect_reconciliation_watcher()

    session.start(_login_request())

    assert monitor.current_pause() is None
    assert not any(isinstance(e, ConnectivityReconciled) for e in event_bus.published)


def test_login_failed_while_reconnecting_records_a_retry_attempt() -> None:
    monitor, _session, event_bus, _clock, scheduler, _state_machine = _setup()
    monitor.remember_login_request(_login_request())
    event_bus.publish(BrokerSessionInvalidated(at=Timestamp.now(), reason="斷線"))
    attempts_before = monitor.reconnect_attempt_count

    event_bus.publish(BrokerLoginFailed(at=Timestamp.now(), reason="逾時", retriable=True))

    assert monitor.is_reconnecting is True
    assert monitor.reconnect_attempt_count == attempts_before + 1
    assert len(scheduler.scheduled) == 2


def test_login_failed_outside_a_reconnect_episode_is_ignored() -> None:
    monitor, _session, event_bus, _clock, scheduler, _state_machine = _setup()

    event_bus.publish(BrokerLoginFailed(at=Timestamp.now(), reason="密碼錯誤", retriable=False))

    assert monitor.is_reconnecting is False
    assert scheduler.scheduled == []


def test_market_data_tick_marks_channel_connected_without_a_freshness_event() -> None:
    monitor, _session, event_bus, _clock, _scheduler, _state_machine = _setup()

    event_bus.publish(
        MarketDataTickReceived(
            at=Timestamp.now(),
            vendor_symbol="TXFH6",
            price=Decimal("18500"),
            size=1,
            serial_no=1,
            exchange_time=Timestamp.now().value.time(),
        )
    )

    health = monitor.channel_health(ChannelId.MARKET_DATA)
    assert health.connected is True
    assert health.last_message_at is not None


@pytest.mark.parametrize("channel", list(ChannelId))
def test_every_channel_starts_disconnected_and_stale(channel: ChannelId) -> None:
    monitor, _session, _event_bus, _clock, _scheduler, _state_machine = _setup()
    health = monitor.channel_health(channel)
    assert health.connected is False
    assert health.is_stale is True
