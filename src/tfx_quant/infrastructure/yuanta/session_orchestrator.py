"""BrokerSessionOrchestrator — the testable core of the Yuanta SPARK API session
lifecycle.

Deliberately pure Python: no `pythonnet`/`clr`, no wx, no real network/CLR I/O. It is
driven entirely by (1) the thin `SparkAdapterPort` protocol it calls out to, and (2)
`handle_*` callback methods that a real adapter (`spark_api_adapter.py`) or a test
double calls when the underlying `OnResponse` dispatch fires. This split is what makes
the login sequencing, retry/backoff, capability tracking, and duplicate/out-of-order
callback guarding fully unit-testable without any real vendor DLL — see
`docs/adr/0004-broker-session-architecture.md`.

SPARK API collapses the legacy OCX API's five-step sequence (trade login -> order
query -> fill query -> position query -> quote login -> subscribe) into one login
covering trading, market data, and reports together — see ADR 0004. This orchestrator
still runs two safety queries (`GetRealReport`, `GetFutStoreSummary`) before declaring
`READY`, preserving the "no unknown orders/positions before startup" safety property
the implementation prompt's global rules require — just against the new, single-session
API shape: 登入 -> (帳號選擇，若回傳超過一組) -> 回報查詢 -> 庫存查詢 -> 行情訂閱 ->
session-ready.

Every `handle_*` callback carries the `generation` the adapter captured when it issued
the underlying request. `generation` bumps on every `start()`/retry attempt; a callback
whose generation doesn't match the current one is stale (from a superseded attempt) and
is ignored. Combined with the internal phase check (a callback is only accepted while
the orchestrator is in the exact phase that expects it), this is what makes duplicate
and out-of-order callbacks safe to ignore rather than corrupt state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Protocol

from tfx_quant.application.events.events import (
    BrokerCapabilitiesChanged,
    BrokerLoggedOut,
    BrokerLoginFailed,
    BrokerLoginSucceeded,
    BrokerLoginTimedOut,
    BrokerSessionInvalidated,
    BrokerSessionReady,
    Event,
    MarketDataTickReceived,
)
from tfx_quant.application.ports.broker_session import (
    LoginRequest,
    LogoutReason,
    SessionCapabilities,
)
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.backoff import BackoffPolicy
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
from tfx_quant.infrastructure.yuanta.errors import (
    AccountSelectionError,
    MarketDataParseError,
    MarketDataSubscriptionError,
)
from tfx_quant.infrastructure.yuanta.market_data_parsing import parse_stock_tick_push

_RETRIABLE_LOGIN_MSG_CODES = frozenset({"0000"})
"""登入 docs page's `MsgCode` table: `0000`=執行失敗 (generic execution failure — the
only one plausibly transient/network-related, so the only one this orchestrator retries
automatically). `0102`(密碼凍結或未啟用)/`0112`(無此權限使用功能) and any other
unrecognized code are treated as non-retriable — retrying a frozen password or a
permissions problem automatically would just repeat a guaranteed failure, and the docs'
"若登入失敗，不允許太頻繁執行登入作業（每4秒1次）" rate limit makes blind retries risky
for an unknown code."""

_LOGIN_SUCCESS_MSG_CODES = frozenset({"0001", "00001"})
"""Both spellings appear across different docs pages' worked Python examples (`if
strMsgCode == '0001' or strMsgCode == '00001'`) — kept as a set rather than picking one,
matching the docs' own defensive handling."""


class SparkAdapterPort(Protocol):
    """Thin wrapper over the SPARK API session object's calls — see
    `spark_api_adapter.py` for the real `pythonnet`-backed implementation and
    `tests/infrastructure/test_broker_session_orchestrator.py` for the fake.

    One adapter now covers what the legacy OCX API needed two separate adapters
    (trade/quote) for — SPARK API is a single unified session."""

    def open_and_login(
        self, credentials: BrokerCredentials, *, generation: int, environment: Environment
    ) -> None: ...
    def disconnect(self) -> None: ...
    def query_real_report(self, account: TradingAccount) -> None: ...
    def query_positions(self, account: TradingAccount) -> None: ...
    def subscribe(self, symbol: str, account: TradingAccount) -> None:
        """`account` is required — SPARK API's `SubscribeStockTick(LoginAcno, ...)`
        takes the account explicitly, unlike the legacy quote OCX's session-implicit
        `AddMktReg`. Raises `MarketDataSubscriptionError` if the vendor synchronously
        rejects the call (`SubscribeStockTick` returning `False`)."""
        ...

    def unsubscribe(self, symbol: str, account: TradingAccount) -> None: ...


class _Cancellable(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """A seam for timers so tests can avoid real sleeping — see `FakeScheduler` in
    the orchestrator test suite."""

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _Cancellable: ...


class EventPublisher(Protocol):
    """Structural stand-in for `EventCoordinator` — anything with a thread-safe
    `publish(event)` works, so tests can substitute a plain recording stub instead of
    spinning up a real coordinator thread."""

    def publish(self, event: Event) -> None: ...


class ThreadingScheduler:
    """Implements `Scheduler` using `threading.Timer` — the real, production seam."""

    def schedule(self, delay_seconds: float, callback: Callable[[], None]) -> _Cancellable:
        timer = threading.Timer(delay_seconds, callback)
        timer.daemon = True
        timer.start()
        return timer


class _Phase(StrEnum):
    IDLE = auto()
    CONNECTING = auto()
    AWAITING_ACCOUNT_SELECTION = auto()
    QUERYING_REPORTS = auto()
    QUERYING_POSITIONS = auto()
    SUBSCRIBING_MARKET_DATA = auto()
    READY = auto()
    FAILED = auto()
    LOGGED_OUT = auto()


@dataclass
class _SessionState:
    accounts: tuple[TradingAccount, ...] = ()
    selected_account: TradingAccount | None = None
    login_done: bool = False
    reports_query_done: bool = False
    positions_query_done: bool = False
    market_data_subscription_attempted: bool = False
    """Set once `_begin_market_data_subscription` has run (after both safety queries
    complete), regardless of whether `market_data_symbols` is empty — this is what
    keeps `market_data` from vacuously becoming `True` the instant login succeeds when
    no startup symbols are configured (an empty-set subset check is trivially true)."""
    subscribed_symbols: set[str] = field(default_factory=set)


class BrokerSessionOrchestrator:
    """Implements `IBrokerSession`."""

    def __init__(
        self,
        *,
        adapter: SparkAdapterPort,
        event_coordinator: EventPublisher,
        market_data_symbols: Sequence[str] = (),
        account_no_hint: str | None = None,
        login_timeout_seconds: float = 15.0,
        backoff_factory: Callable[[], BackoffPolicy] | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        """`market_data_symbols` are already-resolved SPARK API quote symbols (e.g.
        `domain.instrument_master.futures_quote_symbol`'s output) — this orchestrator
        deliberately does not translate an `Instrument`/`ContractMonth` into a vendor
        symbol string itself. An empty sequence means the market-data step is
        vacuously satisfied as soon as the safety queries finish.

        `account_no_hint` — when more than one account is returned and one of them
        matches this account number, it's auto-selected; otherwise `select_account()`
        must be called externally before the session can complete. Sourced by the
        caller from `infrastructure.yuanta.login_preferences` (the previously-selected
        account, remembered non-secretly across runs), not from this class.
        """
        self._adapter = adapter
        self._event_coordinator = event_coordinator
        self._market_data_symbols = tuple(market_data_symbols)
        self._account_no_hint = account_no_hint
        self._login_timeout_seconds = login_timeout_seconds
        self._backoff = (backoff_factory or BackoffPolicy)()
        self._scheduler: Scheduler = scheduler or ThreadingScheduler()

        self._lock = threading.RLock()
        self._phase = _Phase.IDLE
        self._generation = 0
        self._state = _SessionState()
        self._credentials: BrokerCredentials | None = None
        self._environment: Environment | None = None
        self._pending_timer: _Cancellable | None = None
        self._last_capabilities = SessionCapabilities()
        self._ready_published = False

    # -- IBrokerSession -----------------------------------------------------------

    @property
    def capabilities(self) -> SessionCapabilities:
        with self._lock:
            return self._current_capabilities()

    @property
    def accounts(self) -> Sequence[TradingAccount]:
        with self._lock:
            return self._state.accounts

    @property
    def selected_account(self) -> TradingAccount | None:
        with self._lock:
            return self._state.selected_account

    def start(self, request: LoginRequest) -> None:
        with self._lock:
            if self._phase not in (
                _Phase.IDLE,
                _Phase.FAILED,
                _Phase.LOGGED_OUT,
            ):
                return  # already starting/started — not a no-op only when terminal
            self._credentials = BrokerCredentials(
                user_id=request.user_id.strip(), password=request.password
            )
            self._environment = request.environment
            self._backoff.reset()
            self._state = _SessionState()
            self._ready_published = False
            self._generation += 1
            self._begin_connect()

    def select_account(self, account: TradingAccount) -> None:
        with self._lock:
            if account not in self._state.accounts:
                raise AccountSelectionError(
                    f"帳號 {account.branch_id}-{account.account_no} 不在登入回傳的帳號清單中"
                )
            if self._phase != _Phase.AWAITING_ACCOUNT_SELECTION:
                raise AccountSelectionError("目前並非等待帳號選擇的狀態，無法選擇帳號")
            self._state.selected_account = account
            self._begin_report_query()

    def cancel_start(self) -> None:
        with self._lock:
            self._backoff.cancel()
            self._cancel_pending_timer()
            if self._phase not in (_Phase.READY, _Phase.LOGGED_OUT, _Phase.IDLE):
                self._adapter.disconnect()
                self._phase = _Phase.FAILED
                self._collapse_and_publish_capabilities()

    def subscribe_market_data(self, symbol: str) -> None:
        """Runtime (post-ready) subscribe, distinct from the fixed `market_data_symbols`
        list used during the startup sequence — this is what Feature 03's instrument
        switch flow calls. Only valid while `READY`; deliberately does not touch
        `_market_data_symbols` (the startup-capability comparison set), so this never
        perturbs `_current_capabilities()`'s `market_data` flag."""
        with self._lock:
            if self._phase != _Phase.READY:
                raise MarketDataSubscriptionError("尚未完成登入與初始行情訂閱，無法訂閱新商品行情")
            assert self._state.selected_account is not None
            self._adapter.subscribe(symbol, self._state.selected_account)
            self._state.subscribed_symbols.add(symbol)

    def unsubscribe_market_data(self, symbol: str) -> None:
        """Safe to call for a symbol that was never subscribed — a no-op, matching
        `IBrokerSession.unsubscribe_market_data`'s documented contract."""
        with self._lock:
            if self._phase != _Phase.READY:
                return
            assert self._state.selected_account is not None
            self._adapter.unsubscribe(symbol, self._state.selected_account)
            self._state.subscribed_symbols.discard(symbol)

    def stop(self) -> None:
        with self._lock:
            self._cancel_pending_timer()
            self._backoff.cancel()

            if self._state.selected_account is not None and self._phase in (
                _Phase.READY,
                _Phase.QUERYING_REPORTS,
                _Phase.QUERYING_POSITIONS,
                _Phase.SUBSCRIBING_MARKET_DATA,
            ):
                # Best-effort final order-status check. Feature 02 has no order
                # cancellation/resend capability (that's Feature 06+), so this can
                # only surface an ambiguous state, not resolve it — it deliberately
                # does not claim a clean shutdown by skipping the check.
                self._adapter.query_real_report(self._state.selected_account)

            if self._state.selected_account is not None:
                for symbol in list(self._state.subscribed_symbols):
                    self._adapter.unsubscribe(symbol, self._state.selected_account)

            self._adapter.disconnect()

            self._phase = _Phase.LOGGED_OUT
            self._state = _SessionState()
            # Per the login-input implementation prompt: credentials only live in
            # memory for as long as the current login needs them, cleared on logout.
            self._credentials = None
            self._collapse_and_publish_capabilities()
            self._publish(BrokerLoggedOut(at=Timestamp.now(), reason=LogoutReason.USER_REQUESTED))

    # -- Login ----------------------------------------------------------------------

    def _begin_connect(self) -> None:
        self._phase = _Phase.CONNECTING
        assert self._credentials is not None
        assert self._environment is not None
        self._adapter.open_and_login(
            self._credentials, generation=self._generation, environment=self._environment
        )
        self._arm_timeout()

    def handle_login_result(
        self,
        generation: int,
        msg_code: str,
        msg_content: str,
        login_entries: Sequence[tuple[str, str, str, str]],
    ) -> None:
        """`login_entries` is `(Account, Name, InvestorID, SellerNo)` per
        `LoginResult.LoginList` entry — the real adapter extracts these primitives
        from the `.NET` objects so this method (and its tests) stay pure Python."""
        with self._lock:
            if generation != self._generation or self._phase != _Phase.CONNECTING:
                return  # stale/duplicate/out-of-order — ignore

            self._cancel_pending_timer()

            if msg_code in _LOGIN_SUCCESS_MSG_CODES:
                accounts = _parse_login_accounts(login_entries)
                if not accounts:
                    self._handle_startup_failure(
                        "登入成功，但回傳帳號清單無法解析為有效期貨帳號", retriable=False
                    )
                    return
                self._state.accounts = accounts
                self._state.login_done = True
                self._backoff.reset()
                self._publish(BrokerLoginSucceeded(at=Timestamp.now(), accounts=accounts))
                self._publish_capabilities_if_changed()

                resolved = _resolve_account(accounts, self._account_no_hint)
                if resolved is not None:
                    self._state.selected_account = resolved
                    self._begin_report_query()
                else:
                    self._phase = _Phase.AWAITING_ACCOUNT_SELECTION
                return

            reason = msg_content or f"登入失敗（代碼 {msg_code}）"
            retriable = msg_code in _RETRIABLE_LOGIN_MSG_CODES
            self._handle_startup_failure(reason, retriable)

    def handle_session_invalidated(self, generation: int, reason: str) -> None:
        """Best-effort seam for a post-ready session becoming unusable (passive
        disconnect, broker-side error). SPARK API's docs don't document a distinct
        "disconnected" push event the way the legacy quote OCX's `OnMktStatusChange`
        was an ongoing status stream — this method exists so the orchestrator's
        behavior is testable and so a real mechanism, if one is confirmed later, can be
        wired in without changing the orchestrator. See `docs/adr/0004-*.md`."""
        with self._lock:
            if generation != self._generation or self._phase != _Phase.READY:
                return
            self._invalidate_session(reason)

    # -- Safety queries ---------------------------------------------------------

    def _begin_report_query(self) -> None:
        self._phase = _Phase.QUERYING_REPORTS
        assert self._state.selected_account is not None
        self._adapter.query_real_report(self._state.selected_account)

    def handle_real_report_query_result(self, generation: int) -> None:
        """`GetRealReport` (即時回報查詢) — this deliberately does not parse the
        returned `RealReportList` into typed `Order`/`Fill` domain objects: building
        one needs an idempotency key mapping that's Feature 06's job, same posture the
        legacy-API design always took for `ReportQuery`/`DealQuery`. Only completion is
        tracked here, for sequencing/capability purposes."""
        with self._lock:
            if generation != self._generation or self._phase != _Phase.QUERYING_REPORTS:
                return
            self._state.reports_query_done = True
            self._publish_capabilities_if_changed()
            self._begin_position_query()

    def _begin_position_query(self) -> None:
        self._phase = _Phase.QUERYING_POSITIONS
        assert self._state.selected_account is not None
        self._adapter.query_positions(self._state.selected_account)

    def handle_position_query_result(self, generation: int) -> None:
        """`GetFutStoreSummary` (期貨庫存總表查詢) — same "track completion only, don't
        parse into typed `Position` yet" posture as `handle_real_report_query_result`;
        Feature 08 (position reconciliation) is what builds a typed `Position` from
        this."""
        with self._lock:
            if generation != self._generation or self._phase != _Phase.QUERYING_POSITIONS:
                return
            self._state.positions_query_done = True
            self._publish_capabilities_if_changed()
            self._begin_market_data_subscription()

    # -- Market data subscription + push --------------------------------------------

    def _begin_market_data_subscription(self) -> None:
        self._phase = _Phase.SUBSCRIBING_MARKET_DATA
        if not self._market_data_symbols:
            self._state.market_data_subscription_attempted = True
            self._complete_session()
            return
        assert self._state.selected_account is not None
        for symbol in self._market_data_symbols:
            try:
                self._adapter.subscribe(symbol, self._state.selected_account)
            except MarketDataSubscriptionError as exc:
                self._handle_startup_failure(f"商品 {symbol} 行情訂閱失敗：{exc}", retriable=False)
                return
            self._state.subscribed_symbols.add(symbol)
        self._state.market_data_subscription_attempted = True
        self._complete_session()

    def handle_market_data_push(
        self,
        generation: int,
        vendor_symbol: str,
        serial_no: object,
        deal_price: object,
        deal_vol: object,
        hour: object,
        minute: object,
        second: object,
        millisecond: object,
    ) -> None:
        """`SubscribeStockTick`'s ongoing `StockTickResult` push (`OnResponse` with
        `intMark == 2`), forwarded via `spark_api_adapter.py`. Only accepted while
        `READY` — a stale-generation or pre-subscription push is ignored, the same
        guard every other `handle_*` callback here applies. Deliberately does not
        translate `vendor_symbol` into `Instrument`/`ContractMonth` itself — same
        boundary this orchestrator already keeps for `market_data_symbols` (see
        `__init__`'s docstring); `MarketDataBarService` (which already knows the active
        selection) does that translation."""
        with self._lock:
            if generation != self._generation or self._phase != _Phase.READY:
                return
        try:
            parsed = parse_stock_tick_push(
                stk_code=vendor_symbol,
                serial_no=serial_no,
                deal_price=deal_price,
                deal_vol=deal_vol,
                hour=hour,
                minute=minute,
                second=second,
                millisecond=millisecond,
            )
        except MarketDataParseError:
            return  # malformed push — dropped rather than raised into the dispatch loop
        if parsed is None:
            return  # 商品清盤 (settlement) sentinel — nothing to feed downstream
        self._publish(
            MarketDataTickReceived(
                at=Timestamp.now(),
                vendor_symbol=parsed.vendor_symbol,
                price=parsed.price,
                size=parsed.size,
                serial_no=parsed.serial_no,
                exchange_time=parsed.exchange_time,
            )
        )

    def _complete_session(self) -> None:
        assert self._state.selected_account is not None
        self._phase = _Phase.READY
        self._ready_published = True
        self._publish_capabilities_if_changed()
        self._publish(BrokerSessionReady(at=Timestamp.now(), account=self._state.selected_account))

    # -- Failure / retry / invalidation --------------------------------------------

    def _handle_startup_failure(self, reason: str, retriable: bool) -> None:
        self._cancel_pending_timer()
        self._publish(BrokerLoginFailed(at=Timestamp.now(), reason=reason, retriable=retriable))
        self._collapse_and_publish_capabilities()
        self._phase = _Phase.FAILED

        if not retriable or self._backoff.is_cancelled:
            return

        self._backoff.record_failure()
        if self._backoff.is_exhausted:
            return

        delay = self._backoff.next_delay_seconds()
        self._pending_timer = self._scheduler.schedule(delay, self._retry)

    def _retry(self) -> None:
        with self._lock:
            if self._backoff.is_cancelled or self._phase != _Phase.FAILED:
                return
            self._generation += 1
            self._state = _SessionState()
            self._begin_connect()

    def _invalidate_session(self, reason: str) -> None:
        self._publish(BrokerSessionInvalidated(at=Timestamp.now(), reason=reason))
        self._collapse_and_publish_capabilities()
        self._phase = _Phase.FAILED
        self._ready_published = False

    def _on_login_timeout(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if self._phase != _Phase.CONNECTING:
                return
            self._publish(BrokerLoginTimedOut(at=Timestamp.now()))
            self._handle_startup_failure("登入逾時", retriable=True)

    def _arm_timeout(self) -> None:
        generation = self._generation
        self._pending_timer = self._scheduler.schedule(
            self._login_timeout_seconds, lambda: self._on_login_timeout(generation)
        )

    def _cancel_pending_timer(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None

    # -- Capabilities ---------------------------------------------------------------

    def _current_capabilities(self) -> SessionCapabilities:
        queries_done = self._state.reports_query_done and self._state.positions_query_done
        # Subset, not equality: `subscribe_market_data()` (Feature 03's runtime
        # instrument-switch path) may add symbols beyond this fixed startup set —
        # that must not retroactively invalidate the market_data capability.
        symbols_subscribed = set(self._market_data_symbols) <= self._state.subscribed_symbols
        return SessionCapabilities(
            login=self._state.login_done,
            # SPARK API auto-subscribes 即時回報 (RR_RealReport) on login — no
            # separate opt-in call exists (見 回報 > 即時回報訂閱：「登入即訂閱」) — so
            # this capability tracks login itself, not a distinct step.
            order_reports=self._state.login_done,
            market_data=self._state.market_data_subscription_attempted and symbols_subscribed,
            trading=(
                self._state.login_done and self._state.selected_account is not None and queries_done
            ),
            queries=queries_done,
        )

    def _publish_capabilities_if_changed(self) -> None:
        current = self._current_capabilities()
        if current != self._last_capabilities:
            self._last_capabilities = current
            self._publish(BrokerCapabilitiesChanged(at=Timestamp.now(), capabilities=current))

    def _collapse_and_publish_capabilities(self) -> None:
        self._state.login_done = False
        self._state.reports_query_done = False
        self._state.positions_query_done = False
        self._state.market_data_subscription_attempted = False
        self._state.subscribed_symbols = set()
        self._publish_capabilities_if_changed()

    def _publish(self, event: Event) -> None:
        self._event_coordinator.publish(event)


def _parse_login_accounts(
    entries: Sequence[tuple[str, str, str, str]],
) -> tuple[TradingAccount, ...]:
    """Builds `TradingAccount`s from `LoginResult.LoginList` entries — `(Account,
    Name, InvestorID, SellerNo)` tuples, `Account` in SPARK API's documented futures
    format `F + 分公司代號(7+3) + 帳號(7)` (登入 docs page), e.g.
    `"FF0210132243219588"`. Only `F`-prefixed (futures) entries are kept — this
    codebase never trades securities. The `7+3`/`7` split is a best-effort reading of
    the docs' own notation (`分公司代號(7+3)+帳號(7)`), not independently confirmed
    against a real login response this session — see
    `docs/adr/0004-broker-session-architecture.md`'s open-questions section.
    Malformed entries are skipped, not raised on."""
    accounts: list[TradingAccount] = []
    for account_str, _name, _investor_id, _seller_no in entries:
        account_str = account_str.strip()
        if not account_str.startswith("F") or len(account_str) != 18:
            continue  # not a futures account in the documented 18-char (F+17) format
        digits = account_str[1:]
        branch_id, account_no = digits[:10], digits[10:]
        try:
            accounts.append(
                TradingAccount(branch_id=branch_id, account_no=account_no, sub_account="")
            )
        except DomainError:
            continue
    return tuple(accounts)


def _resolve_account(
    accounts: Sequence[TradingAccount], account_no_hint: str | None
) -> TradingAccount | None:
    if len(accounts) == 1:
        return accounts[0]
    if account_no_hint:
        matches = [a for a in accounts if a.account_no == account_no_hint]
        if len(matches) == 1:
            return matches[0]
    return None
