from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import SecretStr

from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerDuplicateLoginRejected,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerLoginSucceeded,
    BrokerLoginTimedOut,
    BrokerSessionInvalidated,
    BrokerSessionReady,
    Event,
    MarketDataTickReceived,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.infrastructure.yuanta.backoff import BackoffPolicy
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials, StaticCredentialSource
from tfx_quant.infrastructure.yuanta.errors import (
    AccountSelectionError,
    MarketDataSubscriptionError,
)
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator

_ACCOUNT_1 = TradingAccount(branch_id="F00", account_no="9808900", sub_account="")
_ACCOUNT_2 = TradingAccount(branch_id="F00", account_no="1234567", sub_account="0001")
_ACC_LIST_ONE = "2-F00-9808900- -路人甲"
_ACC_LIST_TWO = "2-F00-9808900- -路人甲;2-F00-1234567-0001-路人乙"


class FakeTradeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def connect(self, credentials: BrokerCredentials, *, generation: int) -> None:
        self.calls.append(("connect", generation))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def query_open_orders(self, account: TradingAccount) -> None:
        self.calls.append(("query_open_orders", account))

    def query_fills(self, account: TradingAccount) -> None:
        self.calls.append(("query_fills", account))

    def query_positions(self, account: TradingAccount) -> None:
        self.calls.append(("query_positions", account))


class FakeQuoteAdapter:
    def __init__(self, *, subscribe_results: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._subscribe_results = subscribe_results or {}

    def connect(self, credentials: BrokerCredentials, *, generation: int) -> None:
        self.calls.append(("connect", generation))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def subscribe(self, symbol: str) -> int:
        self.calls.append(("subscribe", symbol))
        return self._subscribe_results.get(symbol, 0)

    def unsubscribe(self, symbol: str) -> int:
        self.calls.append(("unsubscribe", symbol))
        return 0


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


def _credentials() -> StaticCredentialSource:
    return StaticCredentialSource(BrokerCredentials(user_id="A123456789", password=SecretStr("x")))


def make_orchestrator(
    *,
    market_data_symbols: tuple[str, ...] = (),
    account_no_hint: str | None = None,
    max_attempts: int = 3,
    quote_subscribe_results: dict[str, int] | None = None,
) -> tuple[
    BrokerSessionOrchestrator,
    FakeTradeAdapter,
    FakeQuoteAdapter,
    RecordingEventPublisher,
    FakeScheduler,
]:
    trade = FakeTradeAdapter()
    quote = FakeQuoteAdapter(subscribe_results=quote_subscribe_results)
    events = RecordingEventPublisher()
    scheduler = FakeScheduler()
    orchestrator = BrokerSessionOrchestrator(
        trade_adapter=trade,
        quote_adapter=quote,
        credential_source=_credentials(),
        event_coordinator=events,
        market_data_symbols=market_data_symbols,
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
    return orchestrator, trade, quote, events, scheduler


def _drive_to_ready(
    orchestrator: BrokerSessionOrchestrator,
    *,
    generation: int = 1,
    acc_list: str = _ACC_LIST_ONE,
) -> None:
    orchestrator.handle_trade_login_result(generation, 2, acc_list, "", "")
    orchestrator.handle_order_query_result(generation, 0, "")
    orchestrator.handle_deal_query_result(generation, 0, "")
    orchestrator.handle_user_defined_func_result(generation, 0, "RETC=00000", "RA003")
    orchestrator.handle_quote_status_changed(generation, 2, "0")


# -- Success ----------------------------------------------------------------------


def test_full_success_sequence_reaches_session_ready() -> None:
    orchestrator, trade, quote, events, _scheduler = make_orchestrator()
    orchestrator.start()

    assert trade.calls == [("connect", 1)]

    _drive_to_ready(orchestrator)

    assert orchestrator.capabilities.is_session_ready
    assert orchestrator.selected_account == _ACCOUNT_1
    ready_events = events.of_type(BrokerSessionReady)
    assert len(ready_events) == 1
    assert ready_events[0].account == _ACCOUNT_1  # type: ignore[attr-defined]
    assert trade.calls == [
        ("connect", 1),
        ("query_open_orders", _ACCOUNT_1),
        ("query_fills", _ACCOUNT_1),
        ("query_positions", _ACCOUNT_1),
    ]
    assert quote.calls == [("connect", 1)]


def test_success_with_market_data_symbols_subscribes_each_before_ready() -> None:
    orchestrator, trade, quote, events, _scheduler = make_orchestrator(
        market_data_symbols=("TXFE9", "MXFE9")
    )
    orchestrator.start()
    _drive_to_ready(orchestrator)

    assert orchestrator.capabilities.is_session_ready
    assert quote.calls == [("connect", 1), ("subscribe", "TXFE9"), ("subscribe", "MXFE9")]


def test_multiple_accounts_require_explicit_selection() -> None:
    orchestrator, trade, quote, events, _scheduler = make_orchestrator()
    orchestrator.start()

    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_TWO, "", "")

    assert orchestrator.accounts == (_ACCOUNT_1, _ACCOUNT_2)
    assert orchestrator.selected_account is None
    assert trade.calls == [("connect", 1)]  # no query issued yet

    orchestrator.select_account(_ACCOUNT_2)

    assert orchestrator.selected_account == _ACCOUNT_2
    assert ("query_open_orders", _ACCOUNT_2) in trade.calls


def test_account_no_hint_auto_selects_among_multiple_accounts() -> None:
    orchestrator, trade, _quote, _events, _scheduler = make_orchestrator(account_no_hint="1234567")
    orchestrator.start()

    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_TWO, "", "")

    assert orchestrator.selected_account == _ACCOUNT_2
    assert ("query_open_orders", _ACCOUNT_2) in trade.calls


def test_select_account_rejects_unknown_account() -> None:
    orchestrator, _trade, _quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_TWO, "", "")

    with pytest.raises(AccountSelectionError):
        orchestrator.select_account(TradingAccount(branch_id="F99", account_no="0000001"))


# -- Timeout ------------------------------------------------------------------------


def test_login_timeout_publishes_event_and_retries_with_backoff() -> None:
    orchestrator, trade, _quote, events, scheduler = make_orchestrator(max_attempts=3)
    orchestrator.start()

    scheduler.fire_latest()  # fires the armed login timeout

    assert len(events.of_type(BrokerLoginTimedOut)) == 1
    assert len(events.of_type(BrokerLoginFailed)) == 1
    assert trade.calls == [("connect", 1)]  # retry not yet fired

    scheduler.fire_latest()  # fires the scheduled retry

    assert trade.calls == [("connect", 1), ("connect", 2)]


def test_timeout_after_cancel_start_does_not_retry() -> None:
    orchestrator, trade, _quote, _events, scheduler = make_orchestrator()
    orchestrator.start()
    orchestrator.cancel_start()

    scheduler.fire_latest()  # the (already-cancelled) login timeout timer

    assert trade.calls.count(("connect", 1)) == 1
    assert all(call[0] != "connect" or call[1] != 2 for call in trade.calls)


# -- Error codes ----------------------------------------------------------------------


@pytest.mark.parametrize("tlink_status", [4, 5, -100, -10])
def test_non_retriable_trade_login_failure_does_not_schedule_retry(tlink_status: int) -> None:
    orchestrator, trade, _quote, events, scheduler = make_orchestrator(max_attempts=5)
    orchestrator.start()

    orchestrator.handle_trade_login_result(1, tlink_status, "", "", "")

    failures = events.of_type(BrokerLoginFailed)
    assert len(failures) == 1
    assert failures[0].retriable is False  # type: ignore[attr-defined]
    assert scheduler.pending_count == 0  # no timeout timer left armed, no retry scheduled


@pytest.mark.parametrize("tlink_status", [-1, -2])
def test_retriable_trade_login_failure_schedules_retry(tlink_status: int) -> None:
    orchestrator, trade, _quote, events, scheduler = make_orchestrator(max_attempts=5)
    orchestrator.start()

    orchestrator.handle_trade_login_result(1, tlink_status, "", "", "")

    failures = events.of_type(BrokerLoginFailed)
    assert failures[0].retriable is True  # type: ignore[attr-defined]
    assert scheduler.pending_count == 1

    scheduler.fire_latest()
    assert trade.calls == [("connect", 1), ("connect", 2)]


def test_backoff_exhaustion_stops_retrying() -> None:
    orchestrator, trade, _quote, events, scheduler = make_orchestrator(max_attempts=2)
    orchestrator.start()

    orchestrator.handle_trade_login_result(1, -1, "", "", "")  # attempt 1 fails, retry scheduled
    scheduler.fire_latest()  # attempt 2 begins
    orchestrator.handle_trade_login_result(2, -1, "", "", "")  # attempt 2 fails, exhausted

    assert len(events.of_type(BrokerLoginFailed)) == 2
    assert scheduler.pending_count == 0
    assert trade.calls == [("connect", 1), ("connect", 2)]


# -- Duplicate login ------------------------------------------------------------------


def test_quote_duplicate_login_publishes_dedicated_event() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_login_and_queries_only(orchestrator)

    orchestrator.handle_quote_status_changed(1, -3, "1duplicate")

    duplicate_events = events.of_type(BrokerDuplicateLoginRejected)
    assert len(duplicate_events) == 1
    assert duplicate_events[0].source == "quote"  # type: ignore[attr-defined]
    assert not orchestrator.capabilities.is_session_ready


def _drive_login_and_queries_only(orchestrator: BrokerSessionOrchestrator) -> None:
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")
    orchestrator.handle_order_query_result(1, 0, "")
    orchestrator.handle_deal_query_result(1, 0, "")
    orchestrator.handle_user_defined_func_result(1, 0, "RETC=00000", "RA003")


# -- Duplicate / out-of-order callbacks -----------------------------------------------


def test_duplicate_login_callback_is_ignored() -> None:
    orchestrator, trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()

    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")
    # A second, replayed OnLogonS for the same generation arrives after we've already
    # moved on to querying orders.
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")

    assert len(events.of_type(BrokerLoginSucceeded)) == 1
    assert trade.calls.count(("query_open_orders", _ACCOUNT_1)) == 1


def test_duplicate_query_result_callback_is_ignored() -> None:
    orchestrator, trade, _quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")

    orchestrator.handle_order_query_result(1, 0, "")
    orchestrator.handle_order_query_result(1, 0, "")  # duplicate — already in QUERYING_FILLS

    assert trade.calls.count(("query_fills", _ACCOUNT_1)) == 1


def test_stale_generation_callback_after_retry_is_ignored() -> None:
    orchestrator, trade, _quote, events, scheduler = make_orchestrator(max_attempts=5)
    orchestrator.start()
    orchestrator.handle_trade_login_result(1, -1, "", "", "")  # generation 1 fails, retriable
    scheduler.fire_latest()  # generation 2 begins

    # A stale success for the superseded generation 1 arrives late.
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")

    assert orchestrator.selected_account is None
    assert len(events.of_type(BrokerLoginSucceeded)) == 0

    # The current generation's own result is still accepted normally.
    orchestrator.handle_trade_login_result(2, 2, _ACC_LIST_ONE, "", "")
    assert orchestrator.selected_account == _ACCOUNT_1


def test_out_of_order_callback_wrong_phase_is_ignored() -> None:
    orchestrator, trade, _quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()

    # A fill-query result arrives before login even completed.
    orchestrator.handle_deal_query_result(1, 0, "")

    assert trade.calls == [("connect", 1)]  # nothing progressed


# -- Mid-session disconnect / invalidation --------------------------------------------


def test_post_ready_quote_disconnect_invalidates_session() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)
    assert orchestrator.capabilities.is_session_ready

    orchestrator.handle_quote_status_changed(1, -1, "")

    assert not orchestrator.capabilities.login
    assert not orchestrator.capabilities.is_session_ready
    assert len(events.of_type(BrokerSessionInvalidated)) == 1


def test_post_ready_registration_error_invalidates_session() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator(
        market_data_symbols=("TXFE9",)
    )
    orchestrator.start()
    _drive_to_ready(orchestrator)
    assert orchestrator.capabilities.is_session_ready

    orchestrator.handle_quote_registration_error(1, "TXFE9", 4, 3)

    assert len(events.of_type(BrokerSessionInvalidated)) == 1
    assert not orchestrator.capabilities.is_session_ready


def test_subscription_failure_during_startup_is_a_login_failure() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator(
        market_data_symbols=("TXFE9",), quote_subscribe_results={"TXFE9": 1}
    )
    orchestrator.start()
    _drive_login_and_queries_only(orchestrator)

    orchestrator.handle_quote_status_changed(1, 2, "0")

    failures = events.of_type(BrokerLoginFailed)
    assert len(failures) == 1
    assert not orchestrator.capabilities.is_session_ready


# -- stop() sequencing ------------------------------------------------------------


def test_stop_sequencing_order_when_ready() -> None:
    orchestrator, trade, quote, events, _scheduler = make_orchestrator(
        market_data_symbols=("TXFE9",)
    )
    orchestrator.start()
    _drive_to_ready(orchestrator)
    trade.calls.clear()
    quote.calls.clear()

    orchestrator.stop()

    assert trade.calls == [
        ("query_open_orders", _ACCOUNT_1),
        ("disconnect",),
    ]
    assert quote.calls == [("unsubscribe", "TXFE9"), ("disconnect",)]
    assert len(events.of_type(BrokerLoggedOut)) == 1
    assert not orchestrator.capabilities.is_session_ready


def test_stop_before_start_is_a_safe_no_op() -> None:
    orchestrator, trade, quote, events, _scheduler = make_orchestrator()

    orchestrator.stop()  # should not raise

    assert all(call[0] != "query_open_orders" for call in trade.calls)
    assert trade.calls == [("disconnect",)]
    assert quote.calls == [("disconnect",)]
    assert len(events.of_type(BrokerLoggedOut)) == 1


# -- Capabilities never collapse "logged in" into "can trade" -------------------------


def test_capabilities_independent_before_queries_complete() -> None:
    orchestrator, _trade, _quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")

    capabilities = orchestrator.capabilities
    assert capabilities.login is True
    assert capabilities.order_reports is True
    assert capabilities.trading is False  # queries not done yet
    assert capabilities.queries is False
    assert capabilities.market_data is False
    assert capabilities.is_session_ready is False


def test_capabilities_changed_event_published_on_change() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    orchestrator.handle_trade_login_result(1, 2, _ACC_LIST_ONE, "", "")

    changes = events.of_type(BrokerCapabilitiesChanged)
    assert len(changes) >= 1


# -- Runtime subscribe/unsubscribe (Feature 03's instrument switch flow) --------------


def test_subscribe_market_data_calls_adapter_once_ready() -> None:
    orchestrator, _trade, quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)
    quote.calls.clear()

    orchestrator.subscribe_market_data("MXFU6")

    assert quote.calls == [("subscribe", "MXFU6")]


def test_subscribe_market_data_before_ready_raises() -> None:
    orchestrator, _trade, _quote, _events, _scheduler = make_orchestrator()
    with pytest.raises(MarketDataSubscriptionError):
        orchestrator.subscribe_market_data("MXFU6")


def test_subscribe_market_data_propagates_registration_failure() -> None:
    orchestrator, _trade, _quote, _events, _scheduler = make_orchestrator(
        quote_subscribe_results={"BAD": 1}
    )
    orchestrator.start()
    _drive_to_ready(orchestrator)

    with pytest.raises(MarketDataSubscriptionError):
        orchestrator.subscribe_market_data("BAD")


def test_subscribe_market_data_does_not_perturb_startup_capability_set() -> None:
    orchestrator, _trade, _quote, _events, _scheduler = make_orchestrator(
        market_data_symbols=("TXFE9",)
    )
    orchestrator.start()
    _drive_to_ready(orchestrator)
    assert orchestrator.capabilities.market_data is True

    orchestrator.subscribe_market_data("MXFU6")

    assert orchestrator.capabilities.market_data is True


def test_unsubscribe_market_data_calls_adapter_once_ready() -> None:
    orchestrator, _trade, quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)
    orchestrator.subscribe_market_data("MXFU6")
    quote.calls.clear()

    orchestrator.unsubscribe_market_data("MXFU6")

    assert quote.calls == [("unsubscribe", "MXFU6")]


def test_unsubscribe_market_data_before_ready_is_a_safe_no_op() -> None:
    orchestrator, _trade, quote, _events, _scheduler = make_orchestrator()
    orchestrator.unsubscribe_market_data("MXFU6")  # should not raise
    assert quote.calls == []


def test_unsubscribe_market_data_included_in_stop_teardown() -> None:
    orchestrator, _trade, quote, _events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)
    orchestrator.subscribe_market_data("MXFU6")
    quote.calls.clear()

    orchestrator.stop()

    assert ("unsubscribe", "MXFU6") in quote.calls


# -- Market data push (Feature 04's OnGetMktAll wiring) ------------------------------


def _push(
    orchestrator: BrokerSessionOrchestrator,
    *,
    generation: int = 1,
    symbol: str = "TXFU6",
    match_time: str = "093015",
    match_pri: str = "17500",
    match_qty: str = "1",
    tol_match_qty: str = "42",
) -> None:
    orchestrator.handle_market_data_push(
        generation, symbol, match_time, match_pri, match_qty, tol_match_qty
    )


def test_market_data_push_publishes_tick_once_ready() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)

    _push(orchestrator)

    ticks = events.of_type(MarketDataTickReceived)
    assert len(ticks) == 1
    tick = ticks[0]
    assert tick.vendor_symbol == "TXFU6"  # type: ignore[attr-defined]
    assert tick.price == Decimal("17500")  # type: ignore[attr-defined]
    assert tick.cumulative_volume == 42  # type: ignore[attr-defined]


def test_market_data_push_before_ready_is_ignored() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()

    _push(orchestrator)

    assert events.of_type(MarketDataTickReceived) == []


def test_market_data_push_with_stale_generation_is_ignored() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)

    _push(orchestrator, generation=0)  # a superseded attempt's stale generation

    assert events.of_type(MarketDataTickReceived) == []


def test_market_data_push_pre_market_sentinel_publishes_nothing() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)

    _push(orchestrator, tol_match_qty="-1")

    assert events.of_type(MarketDataTickReceived) == []


def test_market_data_push_malformed_field_is_dropped_not_raised() -> None:
    orchestrator, _trade, _quote, events, _scheduler = make_orchestrator()
    orchestrator.start()
    _drive_to_ready(orchestrator)

    _push(orchestrator, match_pri="not-a-number")  # should not raise

    assert events.of_type(MarketDataTickReceived) == []
