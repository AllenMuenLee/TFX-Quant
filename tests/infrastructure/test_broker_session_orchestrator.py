from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import SecretStr

from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerLoginSucceeded,
    BrokerLoginTimedOut,
    BrokerSessionInvalidated,
    BrokerSessionReady,
    Event,
)
from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.infrastructure.yuanta.backoff import BackoffPolicy
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
from tfx_quant.infrastructure.yuanta.errors import AccountSelectionError
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator

_ACCOUNT_1 = TradingAccount(branch_id="0000000001", account_no="2345678", sub_account="")
_ACCOUNT_2 = TradingAccount(branch_id="0000000002", account_no="7654321", sub_account="")
_ACCOUNT_1_STRING = "F00000000012345678"
_ACCOUNT_2_STRING = "F00000000027654321"
_ENTRIES_ONE = ((_ACCOUNT_1_STRING, "路人甲", "A123456789", "55"),)
_ENTRIES_TWO = (
    (_ACCOUNT_1_STRING, "路人甲", "A123456789", "55"),
    (_ACCOUNT_2_STRING, "路人乙", "B123456789", "56"),
)


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def open_and_login(
        self, credentials: BrokerCredentials, *, generation: int, environment: Environment
    ) -> None:
        self.calls.append(("open_and_login", generation))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def query_real_report(self, account: TradingAccount) -> None:
        self.calls.append(("query_real_report", account))

    def query_positions(self, account: TradingAccount) -> None:
        self.calls.append(("query_positions", account))


class _FakeCancellable:
    def __init__(self) -> None:
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeScheduler:
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, Callable[[], None], _FakeCancellable]] = []

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _FakeCancellable:
        cancellable = _FakeCancellable()
        self.scheduled.append((delay_seconds, callback, cancellable))
        return cancellable

    def fire_latest(self) -> None:
        _delay, callback, cancellable = self.scheduled[-1]
        cancellable.fired = True
        if not cancellable.cancelled:
            callback()

    @property
    def pending_count(self) -> int:
        return sum(1 for *_rest, c in self.scheduled if not c.cancelled and not c.fired)


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: type[Event]) -> list[Event]:
        return [e for e in self.events if isinstance(e, event_type)]


def _login_request(*, environment: Environment = Environment.TEST) -> LoginRequest:
    return LoginRequest(
        environment=environment, user_id="F00000000012345678", password=SecretStr("x")
    )


def make_orchestrator(
    *,
    account_no_hint: str | None = None,
    max_attempts: int = 3,
) -> tuple[BrokerSessionOrchestrator, FakeAdapter, RecordingEventPublisher, FakeScheduler]:
    adapter = FakeAdapter()
    events = RecordingEventPublisher()
    scheduler = FakeScheduler()
    orchestrator = BrokerSessionOrchestrator(
        adapter=adapter,
        event_coordinator=events,
        account_no_hint=account_no_hint,
        login_timeout_seconds=5.0,
        backoff_factory=lambda: BackoffPolicy(
            base_delay_seconds=0.01,
            max_delay_seconds=0.1,
            multiplier=2.0,
            max_attempts=max_attempts,
        ),
        scheduler=scheduler,
    )
    return orchestrator, adapter, events, scheduler


def _drive_to_ready(
    orchestrator: BrokerSessionOrchestrator,
    *,
    generation: int = 1,
    entries: tuple[tuple[str, str, str, str], ...] = _ENTRIES_ONE,
) -> None:
    orchestrator.handle_login_result(generation, "0001", "執行成功!", entries)
    orchestrator.handle_real_report_query_result(generation)
    orchestrator.handle_position_query_result(generation)


# -- Success ----------------------------------------------------------------------


def test_full_success_sequence_reaches_session_ready() -> None:
    orchestrator, adapter, events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())

    assert adapter.calls == [("open_and_login", 1)]

    _drive_to_ready(orchestrator)

    assert orchestrator.capabilities.is_session_ready
    assert orchestrator.selected_account == _ACCOUNT_1
    ready_events = events.of_type(BrokerSessionReady)
    assert len(ready_events) == 1
    assert ready_events[0].account == _ACCOUNT_1  # type: ignore[attr-defined]
    assert adapter.calls == [
        ("open_and_login", 1),
        ("query_real_report", _ACCOUNT_1),
        ("query_positions", _ACCOUNT_1),
    ]


def test_multiple_accounts_require_explicit_selection() -> None:
    orchestrator, adapter, _events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_TWO)

    assert orchestrator.accounts == (_ACCOUNT_1, _ACCOUNT_2)
    assert orchestrator.selected_account is None
    assert adapter.calls == [("open_and_login", 1)]  # no query issued yet

    orchestrator.select_account(_ACCOUNT_2)

    assert orchestrator.selected_account == _ACCOUNT_2
    assert ("query_real_report", _ACCOUNT_2) in adapter.calls


def test_account_no_hint_auto_selects_among_multiple_accounts() -> None:
    orchestrator, adapter, _events, _scheduler = make_orchestrator(account_no_hint="7654321")
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_TWO)

    assert orchestrator.selected_account == _ACCOUNT_2
    assert ("query_real_report", _ACCOUNT_2) in adapter.calls


def test_select_account_rejects_unknown_account() -> None:
    orchestrator, _adapter, _events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_TWO)

    with pytest.raises(AccountSelectionError):
        orchestrator.select_account(TradingAccount(branch_id="9999999999", account_no="0000001"))


# -- Timeout ------------------------------------------------------------------------


def test_login_timeout_publishes_event_and_retries_with_backoff() -> None:
    orchestrator, adapter, events, scheduler = make_orchestrator(max_attempts=3)
    orchestrator.start(_login_request())

    scheduler.fire_latest()  # fires the armed login timeout

    assert len(events.of_type(BrokerLoginTimedOut)) == 1
    assert len(events.of_type(BrokerLoginFailed)) == 1
    assert adapter.calls == [("open_and_login", 1)]  # retry not yet fired

    scheduler.fire_latest()  # fires the scheduled retry

    assert adapter.calls == [("open_and_login", 1), ("open_and_login", 2)]


def test_timeout_after_cancel_start_does_not_retry() -> None:
    orchestrator, adapter, _events, scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    orchestrator.cancel_start()

    scheduler.fire_latest()  # the (already-cancelled) login timeout timer

    assert adapter.calls.count(("open_and_login", 1)) == 1
    assert all(call[0] != "open_and_login" or call[1] != 2 for call in adapter.calls)


# -- MsgCode handling -----------------------------------------------------------------


@pytest.mark.parametrize("msg_code", ["0102", "0112", "9999"])
def test_non_retriable_login_failure_does_not_schedule_retry(msg_code: str) -> None:
    orchestrator, _adapter, events, scheduler = make_orchestrator(max_attempts=5)
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, msg_code, "密碼錯誤或無權限", ())

    failures = events.of_type(BrokerLoginFailed)
    assert len(failures) == 1
    assert failures[0].retriable is False  # type: ignore[attr-defined]
    assert scheduler.pending_count == 0  # no timeout timer left armed, no retry scheduled


def test_retriable_login_failure_schedules_retry() -> None:
    orchestrator, adapter, events, scheduler = make_orchestrator(max_attempts=5)
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, "0000", "執行失敗", ())

    failures = events.of_type(BrokerLoginFailed)
    assert failures[0].retriable is True  # type: ignore[attr-defined]
    assert scheduler.pending_count == 1

    scheduler.fire_latest()
    assert adapter.calls == [("open_and_login", 1), ("open_and_login", 2)]


def test_login_success_with_unparseable_account_list_is_a_failure() -> None:
    orchestrator, _adapter, events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, "0001", "執行成功!", (("S98875005091", "x", "y", "z"),))

    failures = events.of_type(BrokerLoginFailed)
    assert len(failures) == 1
    assert failures[0].retriable is False  # type: ignore[attr-defined]


def test_backoff_exhaustion_stops_retrying() -> None:
    orchestrator, adapter, events, scheduler = make_orchestrator(max_attempts=2)
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, "0000", "執行失敗", ())  # attempt 1 fails, retry scheduled
    scheduler.fire_latest()  # attempt 2 begins
    orchestrator.handle_login_result(2, "0000", "執行失敗", ())  # attempt 2 fails, exhausted

    assert len(events.of_type(BrokerLoginFailed)) == 2
    assert scheduler.pending_count == 0
    assert adapter.calls == [("open_and_login", 1), ("open_and_login", 2)]


# -- Duplicate / out-of-order callbacks -----------------------------------------------


def test_duplicate_login_callback_is_ignored() -> None:
    orchestrator, adapter, events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())

    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_ONE)
    # A second, replayed Login response for the same generation arrives after we've
    # already moved on to querying reports.
    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_ONE)

    assert len(events.of_type(BrokerLoginSucceeded)) == 1
    assert adapter.calls.count(("query_real_report", _ACCOUNT_1)) == 1


def test_duplicate_query_result_callback_is_ignored() -> None:
    orchestrator, adapter, _events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_ONE)

    orchestrator.handle_real_report_query_result(1)
    orchestrator.handle_real_report_query_result(1)  # duplicate — already in QUERYING_POSITIONS

    assert adapter.calls.count(("query_positions", _ACCOUNT_1)) == 1


def test_stale_generation_callback_after_retry_is_ignored() -> None:
    orchestrator, _adapter, events, scheduler = make_orchestrator(max_attempts=5)
    orchestrator.start(_login_request())
    orchestrator.handle_login_result(1, "0000", "執行失敗", ())  # generation 1 fails, retriable
    scheduler.fire_latest()  # generation 2 begins

    # A stale success for the superseded generation 1 arrives late.
    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_ONE)

    assert orchestrator.selected_account is None
    assert len(events.of_type(BrokerLoginSucceeded)) == 0

    # The current generation's own result is still accepted normally.
    orchestrator.handle_login_result(2, "0001", "執行成功!", _ENTRIES_ONE)
    assert orchestrator.selected_account == _ACCOUNT_1


def test_out_of_order_callback_wrong_phase_is_ignored() -> None:
    orchestrator, adapter, _events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())

    # A position-query result arrives before login even completed.
    orchestrator.handle_position_query_result(1)

    assert adapter.calls == [("open_and_login", 1)]  # nothing progressed


# -- Mid-session invalidation ----------------------------------------------------------


def test_session_invalidated_after_ready_collapses_capabilities() -> None:
    orchestrator, _adapter, events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    _drive_to_ready(orchestrator)
    assert orchestrator.capabilities.is_session_ready

    orchestrator.handle_session_invalidated(1, "連線中斷")

    assert not orchestrator.capabilities.login
    assert not orchestrator.capabilities.is_session_ready
    assert len(events.of_type(BrokerSessionInvalidated)) == 1


# -- stop() sequencing ------------------------------------------------------------


def test_stop_sequencing_order_when_ready() -> None:
    orchestrator, adapter, events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    _drive_to_ready(orchestrator)
    adapter.calls.clear()

    orchestrator.stop()

    assert adapter.calls == [
        ("query_real_report", _ACCOUNT_1),
        ("disconnect",),
    ]
    assert len(events.of_type(BrokerLoggedOut)) == 1
    assert not orchestrator.capabilities.is_session_ready


def test_stop_before_start_is_a_safe_no_op() -> None:
    orchestrator, adapter, events, _scheduler = make_orchestrator()

    orchestrator.stop()  # should not raise

    assert all(call[0] != "query_real_report" for call in adapter.calls)
    assert adapter.calls == [("disconnect",)]
    assert len(events.of_type(BrokerLoggedOut)) == 1


# -- Capabilities never collapse "logged in" into "can trade" -------------------------


def test_capabilities_independent_before_queries_complete() -> None:
    orchestrator, _adapter, _events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_ONE)

    capabilities = orchestrator.capabilities
    assert capabilities.login is True
    assert capabilities.order_reports is True
    assert capabilities.trading is False  # queries not done yet
    assert capabilities.queries is False
    assert capabilities.is_session_ready is False


def test_capabilities_changed_event_published_on_change() -> None:
    orchestrator, _adapter, events, _scheduler = make_orchestrator()
    orchestrator.start(_login_request())
    orchestrator.handle_login_result(1, "0001", "執行成功!", _ENTRIES_ONE)

    changes = events.of_type(BrokerCapabilitiesChanged)
    assert len(changes) >= 1
