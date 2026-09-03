"""ConnectivityMonitor — the connectivity health / reconnect / safe-pause coordinator
(implementation prompt 09).

Tracks each of the five channels (`domain.connectivity.ChannelId`) independently rather
than collapsing them into one boolean, drives `StrategyStateMachine` toward
`PAUSED_SAFE`/`FAULTED` itself the moment any of the five documented trigger conditions
fires (market data stale, order-report channel interrupted, trade channel invalid, a
query failed, or the local clock disagrees with a broker-stamped timestamp by too much),
and — the one genuinely new capability this feature adds on top of everything Feature
06/07/08 already wire to `BrokerSessionReady` — retries a passive post-ready disconnect
with a capped, jittered, cancellable backoff by calling `IBrokerSession.start()` again
with the operator's last-used `LoginRequest`.

Never resumes `RUNNING` itself. Reaching `PAUSED_SAFE` — even once every channel is
healthy again and a fresh `BrokerSessionReady` has re-triggered `OrderManager`/
`PositionReconciliationService` and the quote recorder's own reconnect-
reconciliation — still requires a human to restart the strategy through `Starting`'s
full safety checklist, exactly like every other safe-pause path in this codebase.
`OrderManager`'s existing timeout/reconciliation logic already guarantees an order that
was in flight when the disconnect happened is only ever resolved by a matching broker
report/fill or left `UNKNOWN` for manual review — this coordinator adds nothing that
could resend it, so "重連前曾提交但未確定的委託一律保持 Unknown...不因 client timeout
自動重送" holds structurally, by omission.

See `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerLoginTimedOut,
    BrokerSessionInvalidated,
    BrokerSessionReady,
    ChannelHealthChanged,
    ConnectivityReconciled,
    Event,
    FillReceived,
    MarketDataFreshnessChanged,
    OrderReportReceived,
    SafePauseTriggered,
)
from tfx_quant.application.ports.broker_session import IBrokerSession, LoginRequest
from tfx_quant.application.ports.clock import Clock
from tfx_quant.domain.connectivity import (
    ChannelHealth,
    ChannelId,
    SafePauseReason,
    SafePauseRecord,
    clock_skew_seconds,
)
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.reconnect_backoff import ReconnectBackoffPolicy
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine, attempt_safe_pause
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import (
    get_logger,
    log_debug,
    log_error,
    log_info,
    log_warning,
    new_correlation_id,
)

_logger = get_logger(__name__)

_DEFAULT_MAX_CLOCK_SKEW_SECONDS = 5.0
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0

OrderSummaryProvider = Callable[[], int]
PositionSummaryProvider = Callable[[], NetPosition | None]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as
    `application.order_management.order_manager.EventBus`."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


class _Cancellable(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """A seam for the reconnect backoff's variable, per-attempt delay so tests can
    avoid real sleeping across several attempts — the same role
    `infrastructure.yuanta.session_orchestrator.Scheduler` plays for login retries,
    duplicated here (rather than imported) because `application` code must not depend
    on `infrastructure` — see `docs/adr/0003-layering-and-event-threading-model.md`."""

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _Cancellable: ...


class ThreadingScheduler:
    """Implements `Scheduler` using `threading.Timer` — the real, production seam."""

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _Cancellable:
        timer = threading.Timer(delay_seconds, callback)
        timer.daemon = True
        timer.start()
        return timer


class ConnectivityMonitor:
    """Implements `application.connectivity.gateway_tracking.QueryObserver`."""

    def __init__(
        self,
        *,
        broker_session: IBrokerSession,
        strategy_state_machine: StrategyStateMachine,
        clock: Clock,
        event_bus: EventBus,
        order_summary_provider: OrderSummaryProvider = lambda: 0,
        position_summary_provider: PositionSummaryProvider = lambda: None,
        max_clock_skew_seconds: float = _DEFAULT_MAX_CLOCK_SKEW_SECONDS,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        backoff_factory: Callable[[], ReconnectBackoffPolicy] = ReconnectBackoffPolicy,
        random_fn: Callable[[], float] = random.random,
        scheduler: Scheduler | None = None,
    ) -> None:
        """`broker_session` is the *raw* (unwrapped) session — this coordinator issues
        its own `start()` calls directly against it for reconnect attempts, distinct
        from `gateway_tracking.ConnectivityTrackingBrokerSession` (which wraps this same
        `broker_session` for every *other* caller, purely to observe/remember
        operator-initiated `start()` calls). See `desktop/composition.py`'s wiring
        order for why this avoids a construction cycle."""
        self._broker_session = broker_session
        self._strategy_state_machine = strategy_state_machine
        self._clock = clock
        self._event_bus = event_bus
        self._order_summary_provider = order_summary_provider
        self._position_summary_provider = position_summary_provider
        self._max_clock_skew_seconds = max_clock_skew_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._backoff_factory = backoff_factory
        self._random_fn = random_fn
        self._scheduler: Scheduler = scheduler or ThreadingScheduler()

        self._lock = threading.RLock()
        self._health: dict[ChannelId, ChannelHealth] = {
            c: ChannelHealth.initial(c) for c in ChannelId
        }
        self._current_pause: SafePauseRecord | None = None
        self._login_request: LoginRequest | None = None
        self._reconnect_in_progress = False
        self._backoff: ReconnectBackoffPolicy | None = None
        self._pending_timer: _Cancellable | None = None
        self._blocked_intent_count = 0
        self._timer: threading.Timer | None = None
        self._running = False

        event_bus.subscribe(BrokerCapabilitiesChanged, self._on_capabilities_changed)
        # Subscribed unconditionally (unlike `_on_session_ready_reconciled` below) —
        # channel-health reset and reconnect-success bookkeeping must never depend on
        # whether/when `attach_reconnect_reconciliation_watcher()` gets called.
        event_bus.subscribe(BrokerSessionReady, self._on_session_ready_core)
        event_bus.subscribe(BrokerSessionInvalidated, self._on_session_invalidated)
        event_bus.subscribe(BrokerLoggedOut, self._on_logged_out)
        event_bus.subscribe(BrokerLoginFailed, self._on_login_failed)
        event_bus.subscribe(BrokerLoginTimedOut, self._on_login_timed_out)
        event_bus.subscribe(MarketDataFreshnessChanged, self._on_market_data_freshness_changed)
        event_bus.subscribe(OrderReportReceived, self._on_order_report)
        event_bus.subscribe(FillReceived, self._on_fill)

    def attach_reconnect_reconciliation_watcher(self) -> None:
        """Subscribes this monitor's *second* `BrokerSessionReady` handler — the one
        that marks an existing `SafePauseRecord` reconciled and publishes
        `ConnectivityReconciled` — call this LAST in `desktop/composition.py`, strictly
        after `OrderManager`, `PositionReconciliationService`, and the quote recorder
        have all been constructed (each subscribes its own `BrokerSessionReady` handler
        in its own `__init__`).

        `EventCoordinator` dispatches every handler for one event, in subscription
        order, on a single consumer thread (see `application.events.event_coordinator.
        EventCoordinator`'s own docstring) — subscribing last is what lets this handler
        observe that every other synchronous reconnect-reconciliation call
        (`OrderManager.reconcile_on_startup`, `PositionReconciliationService.
        reconcile(RECONNECT)`) has already run for this same event before marking the
        episode reconciled. Deliberately a *separate* handler from
        `_on_session_ready_core` (subscribed unconditionally, above) — channel-health
        reset and reconnect-success bookkeeping must never depend on whether/when this
        method gets called. See `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`."""
        self._event_bus.subscribe(BrokerSessionReady, self._on_session_ready_reconciled)

    # -- Lifecycle: the periodic heartbeat stamp --------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._schedule_next_heartbeat_tick()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._cancel_reconnect_locked(reason="monitor_stopped")

    def _schedule_next_heartbeat_tick(self) -> None:
        timer = threading.Timer(self._heartbeat_interval_seconds, self._on_heartbeat_timer_fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_heartbeat_timer_fire(self) -> None:
        self.on_clock_tick()
        with self._lock:
            if self._running:
                self._schedule_next_heartbeat_tick()

    def on_clock_tick(self, now: Timestamp | None = None) -> None:
        """Stamps `last_heartbeat_at` on every channel — "we actively evaluated this
        channel's health at this instant" — distinct from `last_message_at` ("the last
        time this channel produced real data"). Deliberately never changes
        `connected`/`is_stale`/`last_error` and never publishes `ChannelHealthChanged`
        on its own: a heartbeat tick alone must never look like real recovery (or a
        false positive) for a channel that is actually still down/stale/paused — see
        `docs/adr/0011-connectivity-reconnect-and-safe-pause.md`'s "heartbeat 假陽性"
        discussion. Public so tests can drive it directly, same convention as
        `OrderManager.on_clock_tick`/`MarketDataBarService.on_clock_tick`."""
        resolved_now = now if now is not None else self._clock.now()
        with self._lock:
            for channel, current in self._health.items():
                self._health[channel] = replace(current, last_heartbeat_at=resolved_now)

    # -- Query surface for the UI / composition ---------------------------------------

    def channel_health(self, channel: ChannelId) -> ChannelHealth:
        with self._lock:
            return self._health[channel]

    def all_channel_health(self) -> Mapping[ChannelId, ChannelHealth]:
        with self._lock:
            return dict(self._health)

    def current_pause(self) -> SafePauseRecord | None:
        with self._lock:
            return self._current_pause

    @property
    def is_reconnecting(self) -> bool:
        with self._lock:
            return self._reconnect_in_progress

    @property
    def reconnect_attempt_count(self) -> int:
        with self._lock:
            return self._backoff.attempt_count if self._backoff is not None else 0

    def note_blocked_intent(self, reason: str) -> None:
        """Documented wiring point for a future strategy engine (Feature 05/12 — see
        every other feature's "no automatic caller yet" gap) that finds `StrategyState
        != RUNNING` and declines to submit — increments the audit count a *future*
        `SafePauseRecord` would carry. No current code path calls this automatically."""
        with self._lock:
            self._blocked_intent_count += 1
            log_info(
                _logger,
                "connectivity_blocked_intent_recorded",
                reason=reason,
                total_blocked=self._blocked_intent_count,
            )

    def cancel_reconnect(self) -> None:
        """ "允許使用者停止" — the operator-facing cancel action."""
        with self._lock:
            self._broker_session.cancel_start()
            self._cancel_reconnect_locked(reason="user_requested")

    # -- QueryObserver (called by ConnectivityTrackingTradeGateway) ------------------

    def record_query_result(
        self,
        *,
        call: str,
        ok: bool,
        latency_ms: float,
        error: str | None,
        positions: Sequence[Position] = (),
    ) -> None:
        now = self._clock.now()
        with self._lock:
            self._update_channel(
                ChannelId.QUERIES,
                connected=ok,
                at=now,
                latency_ms=latency_ms,
                error=None if ok else error,
                message=True,
            )
            if not ok:
                log_error(_logger, "connectivity_query_failed", call=call, error=error)
                self._trigger_pause(
                    reason=SafePauseReason.QUERY_FAILED,
                    channel=ChannelId.QUERIES,
                    detail=f"{call} failed: {error}",
                    at=now,
                )
                return
            for position in positions:
                self._check_clock_skew(ChannelId.QUERIES, local_now=now, remote_at=position.as_of)

    def record_trade_result(
        self, *, call: str, ok: bool, latency_ms: float, error: str | None
    ) -> None:
        now = self._clock.now()
        with self._lock:
            current = self._health[ChannelId.TRADE]
            self._update_channel(
                ChannelId.TRADE,
                connected=current.connected,
                at=now,
                latency_ms=latency_ms,
                error=None if ok else error,
            )

    def remember_login_request(self, request: LoginRequest) -> None:
        with self._lock:
            self._login_request = request

    def forget_login_request(self) -> None:
        with self._lock:
            self._login_request = None
            self._cancel_reconnect_locked(reason="explicit_stop")

    # -- Event handlers: session lifecycle --------------------------------------------

    def _on_capabilities_changed(self, event: BrokerCapabilitiesChanged) -> None:
        now = self._clock.now()
        caps = event.capabilities
        with self._lock:
            self._update_channel(ChannelId.LOGIN, connected=caps.login, at=now)
            self._update_channel(ChannelId.QUERIES, connected=caps.queries, at=now)
            was_trade_connected = self._health[ChannelId.TRADE].connected
            was_reports_connected = self._health[ChannelId.ORDER_REPORTS].connected
            self._update_channel(ChannelId.TRADE, connected=caps.trading, at=now)
            self._update_channel(ChannelId.ORDER_REPORTS, connected=caps.order_reports, at=now)
            if was_trade_connected and not caps.trading:
                self._trigger_pause(
                    reason=SafePauseReason.TRADE_CHANNEL_INVALID,
                    channel=ChannelId.TRADE,
                    detail="broker capabilities: trading capability lost",
                    at=now,
                )
            if was_reports_connected and not caps.order_reports:
                self._trigger_pause(
                    reason=SafePauseReason.ORDER_REPORTS_INTERRUPTED,
                    channel=ChannelId.ORDER_REPORTS,
                    detail="broker capabilities: order_reports capability lost",
                    at=now,
                )

    def _on_session_ready_core(self, _event: BrokerSessionReady) -> None:
        now = self._clock.now()
        with self._lock:
            for channel in ChannelId:
                self._update_channel(channel, connected=True, at=now)
            if self._reconnect_in_progress:
                self._cancel_pending_timer()
                attempt = self._backoff.attempt_count if self._backoff is not None else 0
                self._reconnect_in_progress = False
                self._backoff = None
                log_info(
                    _logger,
                    "reconnect_succeeded",
                    attempt=attempt,
                    correlation_id=self._current_pause_correlation_id(),
                )

    def _on_session_ready_reconciled(self, event: BrokerSessionReady) -> None:
        # Subscribed last in `desktop/composition.py`, via
        # `attach_reconnect_reconciliation_watcher()` (see
        # docs/adr/0011-connectivity-reconnect-and-safe-pause.md) — by the time this
        # handler runs, OrderManager/PositionReconciliationService/
        # the quote recorder's own `BrokerSessionReady` handlers (whose
        # order/fill/position reconciliation calls are synchronous, all on this same
        # EventCoordinator dispatch thread) have already run for this event.
        now = self._clock.now()
        with self._lock:
            if self._current_pause is not None and not self._current_pause.reconciled:
                reconciled_record = replace(self._current_pause, reconciled=True)
                self._current_pause = reconciled_record
                log_info(
                    _logger,
                    "connectivity_reconciled",
                    correlation_id=reconciled_record.correlation_id,
                    account_no=str(event.account.account_no),
                )
                self._publish(ConnectivityReconciled(at=now, record=reconciled_record))

    def _on_session_invalidated(self, event: BrokerSessionInvalidated) -> None:
        now = event.at
        with self._lock:
            for channel in (
                ChannelId.LOGIN,
                ChannelId.TRADE,
                ChannelId.ORDER_REPORTS,
                ChannelId.QUERIES,
            ):
                self._update_channel(channel, connected=False, error=event.reason, at=now)
            self._trigger_pause(
                reason=SafePauseReason.TRADE_CHANNEL_INVALID,
                channel=ChannelId.TRADE,
                detail=event.reason,
                at=now,
            )
            self._begin_reconnect()

    def _on_logged_out(self, _event: BrokerLoggedOut) -> None:
        # `BrokerLoggedOut` is only ever published by `IBrokerSession.stop()` — always
        # user-initiated (see `application.events.events.BrokerLoggedOut`) — never
        # auto-reconnect after an intentional disconnect.
        with self._lock:
            self._cancel_reconnect_locked(reason="user_disconnected")

    def _on_login_failed(self, event: BrokerLoginFailed) -> None:
        with self._lock:
            if not self._reconnect_in_progress:
                return
            log_warning(
                _logger,
                "reconnect_attempt_failed",
                attempt=self._backoff.attempt_count if self._backoff is not None else 0,
                reason=event.reason,
                correlation_id=self._current_pause_correlation_id(),
            )
            self._schedule_reconnect_attempt()

    def _on_login_timed_out(self, _event: BrokerLoginTimedOut) -> None:
        with self._lock:
            if not self._reconnect_in_progress:
                return
            log_warning(
                _logger,
                "reconnect_attempt_failed",
                attempt=self._backoff.attempt_count if self._backoff is not None else 0,
                reason="login_timed_out",
                correlation_id=self._current_pause_correlation_id(),
            )
            self._schedule_reconnect_attempt()

    # -- Event handlers: market data ---------------------------------------------------

    def _on_market_data_freshness_changed(self, event: MarketDataFreshnessChanged) -> None:
        now = event.at
        with self._lock:
            self._update_channel(
                ChannelId.MARKET_DATA, connected=not event.is_stale, stale=event.is_stale, at=now
            )
            if event.is_stale:
                self._trigger_pause(
                    reason=SafePauseReason.MARKET_DATA_STALE,
                    channel=ChannelId.MARKET_DATA,
                    detail=f"{event.instrument.value} {event.contract.code} market data went stale",
                    at=now,
                )

    # -- Event handlers: order reports / fills (also the clock-skew signal) ----------

    def _on_order_report(self, event: OrderReportReceived) -> None:
        self._observe_broker_message(broker_at=event.report.at)

    def _on_fill(self, event: FillReceived) -> None:
        self._observe_broker_message(broker_at=event.fill.at)

    def _observe_broker_message(self, *, broker_at: Timestamp) -> None:
        now = self._clock.now()
        with self._lock:
            self._update_channel(ChannelId.ORDER_REPORTS, connected=True, at=now, message=True)
            self._check_clock_skew(ChannelId.ORDER_REPORTS, local_now=now, remote_at=broker_at)

    def _check_clock_skew(
        self, channel: ChannelId, *, local_now: Timestamp, remote_at: Timestamp
    ) -> None:
        skew = clock_skew_seconds(local_now, remote_at)
        if skew > self._max_clock_skew_seconds:
            log_error(
                _logger,
                "connectivity_clock_skew_detected",
                channel=channel.value,
                skew_seconds=skew,
                max_skew_seconds=self._max_clock_skew_seconds,
            )
            self._trigger_pause(
                reason=SafePauseReason.CLOCK_SKEW,
                channel=channel,
                detail=f"local clock differs from broker-reported time by {skew:.1f}s",
                at=local_now,
            )

    # -- Channel health bookkeeping ----------------------------------------------------

    def _update_channel(
        self,
        channel: ChannelId,
        *,
        connected: bool,
        at: Timestamp,
        stale: bool | None = None,
        latency_ms: float | None = None,
        error: str | None = None,
        message: bool = False,
    ) -> ChannelHealth:
        current = self._health[channel]
        new_error = error if error is not None else (None if connected else current.last_error)
        new_is_stale = stale if stale is not None else (not connected)
        new_health = ChannelHealth(
            channel=channel,
            connected=connected,
            last_message_at=at if message else current.last_message_at,
            last_heartbeat_at=current.last_heartbeat_at,
            latency_ms=latency_ms if latency_ms is not None else current.latency_ms,
            last_error=new_error,
            is_stale=new_is_stale,
        )
        self._health[channel] = new_health
        meaningful_change = (
            new_health.connected != current.connected
            or new_health.is_stale != current.is_stale
            or new_health.last_error != current.last_error
        )
        if meaningful_change:
            log_info(
                _logger,
                "connectivity_channel_health_changed",
                channel=channel.value,
                connected=new_health.connected,
                is_stale=new_health.is_stale,
                last_error=new_health.last_error,
            )
            self._publish(ChannelHealthChanged(at=at, channel=channel, health=new_health))
        else:
            log_debug(
                _logger,
                "connectivity_channel_message_observed",
                channel=channel.value,
                latency_ms=latency_ms,
            )
        return new_health

    # -- Safe-pause triggering ----------------------------------------------------------

    def _trigger_pause(
        self, *, reason: SafePauseReason, channel: ChannelId, detail: str, at: Timestamp
    ) -> None:
        current_state = self._strategy_state_machine.state
        if current_state is not StrategyState.RUNNING:
            # Either already paused (never overwrite the first recorded reason — same
            # "only escalate from RUNNING" gate as
            # docs/adr/0010-position-reconciliation-and-manual-sync.md decision 2) or the
            # strategy was never running yet (startup) — no fresh episode to record.
            log_debug(
                _logger,
                "connectivity_pause_trigger_ignored",
                reason=reason.value,
                channel=channel.value,
                current_state=current_state.value,
            )
            return
        correlation_id = new_correlation_id()
        resulting_state = attempt_safe_pause(self._strategy_state_machine)
        effective_at = self._clock.now()
        position_summary = self._position_summary_provider()
        record = SafePauseRecord(
            correlation_id=correlation_id,
            reason=reason,
            channel=channel,
            detail=detail,
            detected_at=at,
            effective_at=effective_at,
            strategy_state_before=current_state,
            resulting_strategy_state=resulting_state,
            active_order_count=self._order_summary_provider(),
            expected_net_lots=position_summary.lots if position_summary is not None else None,
            blocked_intent_count=self._blocked_intent_count,
        )
        self._current_pause = record
        log_error(
            _logger,
            "connectivity_safe_pause_triggered",
            correlation_id=correlation_id,
            reason=reason.value,
            channel=channel.value,
            detail=detail,
            detected_at=at.value.isoformat(),
            effective_at=effective_at.value.isoformat(),
            strategy_state_before=current_state.value,
            resulting_strategy_state=resulting_state.value if resulting_state is not None else None,
            active_order_count=record.active_order_count,
            expected_net_lots=record.expected_net_lots,
            blocked_intent_count=record.blocked_intent_count,
        )
        self._publish(SafePauseTriggered(at=effective_at, record=record))

    # -- Reconnect (capped backoff + jitter + cancellation) ---------------------------

    def _begin_reconnect(self) -> None:
        if self._reconnect_in_progress:
            return
        if self._login_request is None:
            log_warning(_logger, "reconnect_unavailable_no_stored_login_request")
            return
        self._reconnect_in_progress = True
        self._backoff = self._backoff_factory()
        log_info(
            _logger,
            "reconnect_episode_started",
            max_attempts=self._backoff.max_attempts,
            correlation_id=self._current_pause_correlation_id(),
        )
        self._schedule_reconnect_attempt()

    def _schedule_reconnect_attempt(self) -> None:
        backoff = self._backoff
        assert backoff is not None
        correlation_id = self._current_pause_correlation_id()
        if backoff.is_cancelled:
            log_info(
                _logger,
                "reconnect_stopped",
                attempt=backoff.attempt_count,
                stop_reason="cancelled",
                correlation_id=correlation_id,
            )
            self._reconnect_in_progress = False
            return
        backoff.record_failure()
        if backoff.is_exhausted:
            log_error(
                _logger,
                "reconnect_stopped",
                attempt=backoff.attempt_count,
                stop_reason="exhausted",
                correlation_id=correlation_id,
            )
            self._reconnect_in_progress = False
            return
        delay = backoff.next_delay_seconds(random_fn=self._random_fn)
        log_info(
            _logger,
            "reconnect_attempt_scheduled",
            attempt=backoff.attempt_count,
            max_attempts=backoff.max_attempts,
            delay_seconds=delay,
            correlation_id=correlation_id,
        )
        self._pending_timer = self._scheduler.schedule(delay, self._attempt_reconnect)

    def _attempt_reconnect(self) -> None:
        with self._lock:
            backoff = self._backoff
            if backoff is None or backoff.is_cancelled or not self._reconnect_in_progress:
                return
            request = self._login_request
            if request is None:
                self._reconnect_in_progress = False
                return
            attempt = backoff.attempt_count
            correlation_id = self._current_pause_correlation_id()
            log_info(
                _logger, "reconnect_attempt_started", attempt=attempt, correlation_id=correlation_id
            )
        try:
            # Deliberately called without `self._lock` held: `IBrokerSession.start()`
            # may publish events synchronously (the mock session, and any test double
            # using a synchronous event bus) that re-enter this monitor's own handlers
            # (`_on_login_failed`/`_on_session_ready_core`), which themselves acquire the
            # lock — holding it here would deadlock that reentrant case.
            self._broker_session.start(request)
        except Exception as exc:  # noqa: BLE001 - never let a raise kill the reconnect loop
            with self._lock:
                log_error(
                    _logger,
                    "reconnect_attempt_call_failed",
                    attempt=attempt,
                    error=str(exc),
                    correlation_id=correlation_id,
                )
                if self._reconnect_in_progress and self._backoff is backoff:
                    self._schedule_reconnect_attempt()

    def _cancel_reconnect_locked(self, *, reason: str) -> None:
        self._cancel_pending_timer()
        if self._backoff is not None:
            self._backoff.cancel()
        was_in_progress = self._reconnect_in_progress
        self._reconnect_in_progress = False
        self._backoff = None
        if was_in_progress:
            log_info(
                _logger,
                "reconnect_cancelled",
                reason=reason,
                correlation_id=self._current_pause_correlation_id(),
            )

    def _current_pause_correlation_id(self) -> str | None:
        """The active `SafePauseRecord.correlation_id` — this feature's "workflow ID" —
        threaded through every reconnect-attempt log line so a support bundle can trace
        one episode's detection, every retry, and its eventual reconciliation as a
        single unit, per the implementation prompt's "成功後以 workflow ID 記錄..." debug-
        log requirement."""
        return self._current_pause.correlation_id if self._current_pause is not None else None

    def _cancel_pending_timer(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None

    def _publish(self, event: Event) -> None:
        self._event_bus.publish(event)


__all__ = ["ConnectivityMonitor"]
