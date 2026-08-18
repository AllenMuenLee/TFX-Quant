"""BrokerSessionOrchestrator — the testable core of the Yuanta session lifecycle.

Deliberately pure Python: no comtypes, no wx, no real network/COM I/O. It is driven
entirely by (1) the thin `TradeAdapterPort`/`QuoteAdapterPort` protocols it calls out
to, and (2) `handle_*` callback methods that a real adapter (see
`trade_ocx_adapter.py`/`quote_ocx_adapter.py`) or a test double calls when the
underlying vendor control fires an event. This split is what makes the login
sequencing, retry/backoff, capability tracking, and duplicate/out-of-order callback
guarding fully unit-testable without any real OCX — see
`docs/adr/0004-broker-session-architecture.md`.

Sequencing follows the implementation prompt literally: 登入 → 委託查詢 → 成交查詢 →
持倉查詢 → 行情訂閱 → session-ready. Every step must complete before the next begins;
`SessionCapabilities` is only ever all-True once every step has actually succeeded.

Every `handle_*` callback carries the `generation` the adapter captured when it issued
the underlying request. `generation` bumps on every `start()`/retry attempt; a
callback whose generation doesn't match the current one is stale (from a superseded
attempt) and is ignored. Combined with the internal phase check (a callback is only
accepted while the orchestrator is in the exact phase that expects it), this is what
makes duplicate and out-of-order callbacks safe to ignore rather than corrupt state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Protocol

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
from tfx_quant.application.ports.broker_session import LogoutReason, SessionCapabilities
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.backoff import BackoffPolicy
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials, CredentialSource
from tfx_quant.infrastructure.yuanta.errors import (
    AccountSelectionError,
    MarketDataParseError,
    MarketDataSubscriptionError,
)
from tfx_quant.infrastructure.yuanta.market_data_parsing import parse_market_data_push
from tfx_quant.infrastructure.yuanta.vendor_codes import (
    QUOTE_MSG_CODE_DUPLICATE_LOGIN,
    QUOTE_TLINK_IN_PROGRESS,
    QUOTE_TLINK_RETRIABLE,
    QUOTE_TLINK_SUCCESS,
    REG_ERR_SUCCESS,
    TRADE_TLINK_IN_PROGRESS,
    TRADE_TLINK_RETRIABLE,
    TRADE_TLINK_SUCCESS,
    quote_msg_message,
    reg_err_message,
    trade_tlink_message,
)

FUTURES_MARKET_CODE = "2"
"""市場別 code for futures in `OnLogonS`'s `AccList` — see
`infrastructure/yuanta/README.md`."""


class TradeAdapterPort(Protocol):
    """Thin wrapper over the trading OCX's connection/query calls — see
    `trade_ocx_adapter.py` for the real comtypes-backed implementation and
    `tests/infrastructure/test_broker_session_orchestrator.py` for the fake."""

    def connect(self, credentials: BrokerCredentials, *, generation: int) -> None: ...
    def disconnect(self) -> None: ...
    def query_open_orders(self, account: TradingAccount) -> None: ...
    def query_fills(self, account: TradingAccount) -> None: ...
    def query_positions(self, account: TradingAccount) -> None: ...


class QuoteAdapterPort(Protocol):
    """Thin wrapper over the quote OCX's connection/registration calls."""

    def connect(self, credentials: BrokerCredentials, *, generation: int) -> None: ...
    def disconnect(self) -> None: ...
    def subscribe(self, symbol: str) -> int:
        """Returns the vendor's synchronous `RegErrCode` (0 == success)."""
        ...

    def unsubscribe(self, symbol: str) -> int: ...


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
    CONNECTING_TRADE = auto()
    AWAITING_ACCOUNT_SELECTION = auto()
    QUERYING_ORDERS = auto()
    QUERYING_FILLS = auto()
    QUERYING_POSITIONS = auto()
    CONNECTING_QUOTE = auto()
    SUBSCRIBING_MARKET_DATA = auto()
    READY = auto()
    FAILED = auto()
    LOGGED_OUT = auto()


@dataclass
class _SessionState:
    accounts: tuple[TradingAccount, ...] = ()
    selected_account: TradingAccount | None = None
    trade_login_done: bool = False
    orders_done: bool = False
    fills_done: bool = False
    positions_done: bool = False
    quote_login_done: bool = False
    subscribed_symbols: set[str] = field(default_factory=set)


class BrokerSessionOrchestrator:
    """Implements `IBrokerSession`."""

    def __init__(
        self,
        *,
        trade_adapter: TradeAdapterPort,
        quote_adapter: QuoteAdapterPort,
        credential_source: CredentialSource,
        event_coordinator: EventPublisher,
        market_data_symbols: Sequence[str] = (),
        account_no_hint: str | None = None,
        login_timeout_seconds: float = 15.0,
        backoff_factory: Callable[[], BackoffPolicy] | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        """`market_data_symbols` are already-resolved broker-format symbol codes
        (e.g. the EasyWin-style code Feature 03 will produce) — this orchestrator
        deliberately does not translate an `Instrument`/`ContractMonth` into a vendor
        symbol string itself; that mapping isn't documented in the extracted PDFs (see
        `infrastructure/yuanta/README.md`) and guessing it would violate the
        implementation prompt's "不要猜測...欄位" rule. An empty sequence means the
        market-data step is vacuously satisfied as soon as the quote login succeeds.

        `account_no_hint` mirrors `TFX_QUANT_YUANTA_ACCOUNT_NO` (see
        `credentials.py`) — when more than one futures account is returned and one of
        them matches this account number, it's auto-selected; otherwise
        `select_account()` must be called externally before the session can complete.
        """
        self._trade_adapter = trade_adapter
        self._quote_adapter = quote_adapter
        self._credential_source = credential_source
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

    def start(self) -> None:
        with self._lock:
            if self._phase not in (
                _Phase.IDLE,
                _Phase.FAILED,
                _Phase.LOGGED_OUT,
            ):
                return  # already starting/started — not a no-op only when terminal
            self._credentials = self._credential_source.load()
            self._backoff.reset()
            self._state = _SessionState()
            self._ready_published = False
            self._generation += 1
            self._begin_trade_connect()

    def select_account(self, account: TradingAccount) -> None:
        with self._lock:
            if account not in self._state.accounts:
                raise AccountSelectionError(
                    f"帳號 {account.branch_id}-{account.account_no} 不在登入回傳的帳號清單中"
                )
            if self._phase != _Phase.AWAITING_ACCOUNT_SELECTION:
                raise AccountSelectionError("目前並非等待帳號選擇的狀態，無法選擇帳號")
            self._state.selected_account = account
            self._begin_order_query()

    def cancel_start(self) -> None:
        with self._lock:
            self._backoff.cancel()
            self._cancel_pending_timer()
            if self._phase not in (_Phase.READY, _Phase.LOGGED_OUT, _Phase.IDLE):
                self._trade_adapter.disconnect()
                self._quote_adapter.disconnect()
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
                raise MarketDataSubscriptionError(
                    "尚未完成登入與初始行情訂閱，無法訂閱新商品行情"
                )
            reg_err = self._quote_adapter.subscribe(symbol)
            if reg_err != REG_ERR_SUCCESS:
                raise MarketDataSubscriptionError(
                    f"商品 {symbol} 行情註冊失敗：{reg_err_message(reg_err)}"
                )
            self._state.subscribed_symbols.add(symbol)

    def unsubscribe_market_data(self, symbol: str) -> None:
        """Safe to call for a symbol that was never subscribed — a no-op, matching
        `IBrokerSession.unsubscribe_market_data`'s documented contract."""
        with self._lock:
            if self._phase != _Phase.READY:
                return
            self._quote_adapter.unsubscribe(symbol)
            self._state.subscribed_symbols.discard(symbol)

    def stop(self) -> None:
        with self._lock:
            self._cancel_pending_timer()
            self._backoff.cancel()

            if self._state.selected_account is not None and self._phase in (
                _Phase.READY,
                _Phase.QUERYING_ORDERS,
                _Phase.QUERYING_FILLS,
                _Phase.QUERYING_POSITIONS,
                _Phase.CONNECTING_QUOTE,
                _Phase.SUBSCRIBING_MARKET_DATA,
            ):
                # Best-effort final order-status check. Feature 02 has no order
                # cancellation/resend capability (that's Feature 06+), so this can
                # only surface an ambiguous state, not resolve it — it deliberately
                # does not claim a clean shutdown by skipping the check.
                self._trade_adapter.query_open_orders(self._state.selected_account)

            for symbol in list(self._state.subscribed_symbols):
                self._quote_adapter.unsubscribe(symbol)

            self._trade_adapter.disconnect()
            # No documented quote-side logout function exists (see
            # infrastructure/yuanta/README.md) — disconnect() on the real adapter
            # only tears down the local COM object/host, it doesn't call a vendor
            # logout.
            self._quote_adapter.disconnect()

            self._phase = _Phase.LOGGED_OUT
            self._state = _SessionState()
            self._collapse_and_publish_capabilities()
            self._publish(BrokerLoggedOut(at=Timestamp.now(), reason=LogoutReason.USER_REQUESTED))

    # -- Trade login ----------------------------------------------------------------

    def _begin_trade_connect(self) -> None:
        self._phase = _Phase.CONNECTING_TRADE
        assert self._credentials is not None
        self._trade_adapter.connect(self._credentials, generation=self._generation)
        self._arm_timeout()

    def handle_trade_login_result(
        self, generation: int, tlink_status: int, acc_list: str, cert_seq: str, cert_status: str
    ) -> None:
        with self._lock:
            if generation != self._generation or self._phase != _Phase.CONNECTING_TRADE:
                return  # stale/duplicate/out-of-order — ignore
            if tlink_status in TRADE_TLINK_IN_PROGRESS:
                return  # not a terminal result yet

            self._cancel_pending_timer()

            if tlink_status == TRADE_TLINK_SUCCESS:
                accounts = _parse_futures_accounts(acc_list)
                self._state.accounts = accounts
                self._state.trade_login_done = True
                self._backoff.reset()
                self._publish(BrokerLoginSucceeded(at=Timestamp.now(), accounts=accounts))
                self._publish_capabilities_if_changed()

                resolved = _resolve_account(accounts, self._account_no_hint)
                if resolved is not None:
                    self._state.selected_account = resolved
                    self._begin_order_query()
                else:
                    self._phase = _Phase.AWAITING_ACCOUNT_SELECTION
                return

            reason = trade_tlink_message(tlink_status)
            retriable = tlink_status in TRADE_TLINK_RETRIABLE
            self._handle_startup_failure(reason, retriable)

    def handle_trade_disconnected(self, generation: int, tlink_status: int) -> None:
        """Best-effort seam for a passive mid-session disconnect on the trading side.

        The extracted trading-OCX PDF documents no distinct "disconnected" event
        (unlike the quote OCX's `OnMktStatusChange`, which is an ongoing status
        stream) — `OnLogonS` is documented only as a one-shot login-result event. This
        method exists so the orchestrator's behavior is testable and so a future
        confirmed mechanism (e.g. polling `TLinkStatus`, or a vendor event this
        session didn't find) can be wired in without changing the orchestrator. Not
        wired to any real callback yet — see `docs/adr/0004-*.md`.
        """
        with self._lock:
            if generation != self._generation or self._phase != _Phase.READY:
                return
            self._invalidate_session(trade_tlink_message(tlink_status))

    # -- Query sequence ---------------------------------------------------------

    def _begin_order_query(self) -> None:
        self._phase = _Phase.QUERYING_ORDERS
        assert self._state.selected_account is not None
        self._trade_adapter.query_open_orders(self._state.selected_account)

    def handle_order_query_result(self, generation: int, row_count: int, raw: str) -> None:
        with self._lock:
            if generation != self._generation or self._phase != _Phase.QUERYING_ORDERS:
                return
            self._state.orders_done = True
            self._publish_capabilities_if_changed()
            self._phase = _Phase.QUERYING_FILLS
            assert self._state.selected_account is not None
            self._trade_adapter.query_fills(self._state.selected_account)

    def handle_deal_query_result(self, generation: int, row_count: int, raw: str) -> None:
        with self._lock:
            if generation != self._generation or self._phase != _Phase.QUERYING_FILLS:
                return
            self._state.fills_done = True
            self._publish_capabilities_if_changed()
            self._phase = _Phase.QUERYING_POSITIONS
            assert self._state.selected_account is not None
            self._trade_adapter.query_positions(self._state.selected_account)

    def handle_user_defined_func_result(
        self, generation: int, row_count: int, results: str, work_id: str
    ) -> None:
        """`work_id="RA003"` is 部位狀況查詢 (position query) — see README. This
        deliberately does not parse `results` into a structured position: the vendor
        doc gives no worked example for `RA003`'s row layout (unlike `ReportQuery`/
        `DealQuery`, which do), so building a typed `Position` here would mean
        guessing the field order — left for whichever feature confirms it (position
        reconciliation is Feature 08's job)."""
        with self._lock:
            if (
                generation != self._generation
                or self._phase != _Phase.QUERYING_POSITIONS
                or work_id != "RA003"
            ):
                return
            self._state.positions_done = True
            self._publish_capabilities_if_changed()
            self._begin_quote_connect()

    # -- Quote login + subscription -----------------------------------------------

    def _begin_quote_connect(self) -> None:
        self._phase = _Phase.CONNECTING_QUOTE
        assert self._credentials is not None
        self._quote_adapter.connect(self._credentials, generation=self._generation)
        self._arm_timeout()

    def handle_quote_status_changed(self, generation: int, status: int, msg: str) -> None:
        with self._lock:
            if generation != self._generation:
                return

            if self._phase == _Phase.READY:
                if status in QUOTE_TLINK_IN_PROGRESS or status == QUOTE_TLINK_SUCCESS:
                    return
                self._invalidate_session(quote_msg_message(msg))
                return

            if self._phase != _Phase.CONNECTING_QUOTE:
                return  # stale/duplicate/out-of-order
            if status in QUOTE_TLINK_IN_PROGRESS:
                return

            self._cancel_pending_timer()

            if status == QUOTE_TLINK_SUCCESS:
                self._state.quote_login_done = True
                self._publish_capabilities_if_changed()
                self._begin_market_data_subscription()
                return

            if msg and msg[0] == QUOTE_MSG_CODE_DUPLICATE_LOGIN:
                self._publish(BrokerDuplicateLoginRejected(at=Timestamp.now(), source="quote"))
                self._collapse_and_publish_capabilities()
                self._phase = _Phase.FAILED
                return

            retriable = status in QUOTE_TLINK_RETRIABLE
            self._handle_startup_failure(quote_msg_message(msg), retriable)

    def _begin_market_data_subscription(self) -> None:
        self._phase = _Phase.SUBSCRIBING_MARKET_DATA
        if not self._market_data_symbols:
            self._complete_session()
            return
        for symbol in self._market_data_symbols:
            reg_err = self._quote_adapter.subscribe(symbol)
            if reg_err != REG_ERR_SUCCESS:
                self._handle_startup_failure(
                    f"商品 {symbol} 行情註冊失敗：{reg_err_message(reg_err)}", retriable=False
                )
                return
            self._state.subscribed_symbols.add(symbol)
        self._complete_session()

    def handle_quote_registration_error(
        self, generation: int, symbol: str, mode: int, error_code: int
    ) -> None:
        """Async failure notice — per the doc, `OnRegError` "成功則不通知" (only fires
        on failure), so a synchronous `RegErrCode` of 0 from `subscribe()` may still
        be followed by this. Only meaningful while still subscribing; a late arrival
        after `READY` is treated as a market-data disruption."""
        with self._lock:
            if generation != self._generation:
                return
            message = f"商品 {symbol} 行情註冊發生錯誤（代碼 {error_code}）"
            if self._phase == _Phase.SUBSCRIBING_MARKET_DATA:
                self._handle_startup_failure(message, retriable=False)
            elif self._phase == _Phase.READY:
                self._invalidate_session(message)

    # -- Market data --------------------------------------------------------------

    def handle_market_data_push(
        self,
        generation: int,
        symbol: str,
        match_time: str,
        match_pri: str,
        match_qty: str,
        tol_match_qty: str,
    ) -> None:
        """`OnGetMktAll` push, forwarded via `quote_ocx_adapter.py`. Only accepted
        while `READY` — a stale-generation or pre-subscription push is ignored, the
        same guard every other `handle_*` callback here applies. Deliberately does not
        translate `symbol` into `Instrument`/`ContractMonth` itself — same boundary
        this orchestrator already keeps for `market_data_symbols` (see `__init__`'s
        docstring); `MarketDataBarService` (which already knows the active selection)
        does that translation."""
        with self._lock:
            if generation != self._generation or self._phase != _Phase.READY:
                return
        try:
            parsed = parse_market_data_push(
                symbol=symbol,
                match_time=match_time,
                match_pri=match_pri,
                match_qty=match_qty,
                tol_match_qty=tol_match_qty,
            )
        except MarketDataParseError:
            return  # malformed push — dropped rather than raised into a COM callback
        if parsed is None:
            return  # pre-market snapshot (TolMatchQty == -1) — nothing to feed downstream
        self._publish(
            MarketDataTickReceived(
                at=Timestamp.now(),
                vendor_symbol=parsed.vendor_symbol,
                price=parsed.price,
                size=parsed.size,
                cumulative_volume=parsed.cumulative_volume,
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
            self._begin_trade_connect()

    def _invalidate_session(self, reason: str) -> None:
        self._publish(BrokerSessionInvalidated(at=Timestamp.now(), reason=reason))
        self._collapse_and_publish_capabilities()
        self._phase = _Phase.FAILED
        self._ready_published = False

    def _on_login_timeout(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if self._phase not in (_Phase.CONNECTING_TRADE, _Phase.CONNECTING_QUOTE):
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
        queries_done = (
            self._state.orders_done and self._state.fills_done and self._state.positions_done
        )
        # Subset, not equality: `subscribe_market_data()` (Feature 03's runtime
        # instrument-switch path) may add symbols beyond this fixed startup set —
        # that must not retroactively invalidate the market_data capability.
        symbols_subscribed = set(self._market_data_symbols) <= self._state.subscribed_symbols
        return SessionCapabilities(
            login=self._state.trade_login_done,
            market_data=self._state.quote_login_done and symbols_subscribed,
            trading=(
                self._state.trade_login_done
                and self._state.selected_account is not None
                and queries_done
            ),
            order_reports=self._state.trade_login_done,
            queries=queries_done,
        )

    def _publish_capabilities_if_changed(self) -> None:
        current = self._current_capabilities()
        if current != self._last_capabilities:
            self._last_capabilities = current
            self._publish(BrokerCapabilitiesChanged(at=Timestamp.now(), capabilities=current))

    def _collapse_and_publish_capabilities(self) -> None:
        self._state.trade_login_done = False
        self._state.orders_done = False
        self._state.fills_done = False
        self._state.positions_done = False
        self._state.quote_login_done = False
        self._state.subscribed_symbols = set()
        self._publish_capabilities_if_changed()

    def _publish(self, event: Event) -> None:
        self._event_coordinator.publish(event)


def _parse_futures_accounts(acc_list: str) -> tuple[TradingAccount, ...]:
    """Parse `OnLogonS`'s `AccList`, e.g. "2-F00-9808900- -路人甲;2-F00-9808900-0001-
    路人乙" = 市場別-Branch-Account-SubAccount-姓名, keeping only 市場別=='2' (futures)
    entries. Malformed entries are skipped, not raised on — a single bad entry
    shouldn't take down the whole login."""
    accounts: list[TradingAccount] = []
    for entry in acc_list.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("-", 4)
        if len(parts) < 4:
            continue
        market = parts[0]
        if market != FUTURES_MARKET_CODE:
            continue
        branch_id, account_no, sub_account = parts[1], parts[2], parts[3]
        try:
            accounts.append(
                TradingAccount(
                    branch_id=branch_id, account_no=account_no, sub_account=sub_account.strip()
                )
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
