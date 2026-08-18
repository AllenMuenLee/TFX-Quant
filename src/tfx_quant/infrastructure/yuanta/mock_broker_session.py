"""MockBrokerSession — a fully scriptable `IBrokerSession`, no COM/network at all.

Used by `desktop/composition.py` when `TradingSettings.use_mock` is true (the
default), and directly importable by tests that want to drive UI/composition code
through specific session-lifecycle scenarios without the real orchestrator's
generation/phase machinery (that machinery has its own dedicated test suite against
`BrokerSessionOrchestrator` — see `tests/infrastructure/
test_broker_session_orchestrator.py`).

`start()` defaults to an immediate, synchronous happy path (single mock futures
account, all five capabilities become true) so the readiness screen and composition
tests work out of the box; call one of the `simulate_*` methods instead of `start()`
to script a specific failure/edge-case scenario.
"""

from __future__ import annotations

from collections.abc import Sequence

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
)
from tfx_quant.application.ports.broker_session import LogoutReason, SessionCapabilities
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta.errors import AccountSelectionError
from tfx_quant.infrastructure.yuanta.session_orchestrator import EventPublisher

_DEFAULT_MOCK_ACCOUNT = TradingAccount(branch_id="F00", account_no="9808900", sub_account="")


class _NullEventPublisher:
    def publish(self, event: Event) -> None:
        pass


class MockBrokerSession:
    """Implements `IBrokerSession`."""

    def __init__(self, *, event_publisher: EventPublisher | None = None) -> None:
        self._event_publisher: EventPublisher = event_publisher or _NullEventPublisher()
        self._capabilities = SessionCapabilities()
        self._accounts: tuple[TradingAccount, ...] = ()
        self._selected_account: TradingAccount | None = None
        self.subscribed_symbols: set[str] = set()
        """Exposed for test assertions — see `subscribe_market_data`/
        `unsubscribe_market_data`."""

    # -- IBrokerSession -----------------------------------------------------------

    @property
    def capabilities(self) -> SessionCapabilities:
        return self._capabilities

    @property
    def accounts(self) -> Sequence[TradingAccount]:
        return self._accounts

    @property
    def selected_account(self) -> TradingAccount | None:
        return self._selected_account

    def start(self) -> None:
        self.simulate_login_success((_DEFAULT_MOCK_ACCOUNT,))

    def select_account(self, account: TradingAccount) -> None:
        if account not in self._accounts:
            raise AccountSelectionError(
                f"帳號 {account.branch_id}-{account.account_no} 不在登入回傳的帳號清單中"
            )
        self._selected_account = account
        self._set_capabilities(
            SessionCapabilities(
                login=True, market_data=True, trading=True, order_reports=True, queries=True
            )
        )
        self._publish(BrokerSessionReady(at=Timestamp.now(), account=account))

    def cancel_start(self) -> None:
        self._set_capabilities(SessionCapabilities())

    def subscribe_market_data(self, symbol: str) -> None:
        self.subscribed_symbols.add(symbol)

    def unsubscribe_market_data(self, symbol: str) -> None:
        self.subscribed_symbols.discard(symbol)

    def stop(self) -> None:
        self._selected_account = None
        self.subscribed_symbols = set()
        self._set_capabilities(SessionCapabilities())
        self._publish(BrokerLoggedOut(at=Timestamp.now(), reason=LogoutReason.USER_REQUESTED))

    # -- Scripting ------------------------------------------------------------------

    def simulate_login_success(
        self, accounts: Sequence[TradingAccount] = (_DEFAULT_MOCK_ACCOUNT,)
    ) -> None:
        self._accounts = tuple(accounts)
        self._publish(BrokerLoginSucceeded(at=Timestamp.now(), accounts=self._accounts))
        self._set_capabilities(SessionCapabilities(login=True, order_reports=True))
        if len(self._accounts) == 1:
            self.select_account(self._accounts[0])

    def simulate_login_timeout(self) -> None:
        self._publish(BrokerLoginTimedOut(at=Timestamp.now()))
        self._publish(BrokerLoginFailed(at=Timestamp.now(), reason="登入逾時", retriable=True))
        self._set_capabilities(SessionCapabilities())

    def simulate_login_failed(self, reason: str, *, retriable: bool = False) -> None:
        self._publish(BrokerLoginFailed(at=Timestamp.now(), reason=reason, retriable=retriable))
        self._set_capabilities(SessionCapabilities())

    def simulate_duplicate_login(self, *, source: str = "quote") -> None:
        self._publish(BrokerDuplicateLoginRejected(at=Timestamp.now(), source=source))
        self._set_capabilities(SessionCapabilities())

    def simulate_session_invalidated(self, reason: str) -> None:
        self._publish(BrokerSessionInvalidated(at=Timestamp.now(), reason=reason))
        self._selected_account = None
        self._set_capabilities(SessionCapabilities())

    # -- Internal ---------------------------------------------------------------

    def _set_capabilities(self, capabilities: SessionCapabilities) -> None:
        if capabilities != self._capabilities:
            self._capabilities = capabilities
            self._publish(BrokerCapabilitiesChanged(at=Timestamp.now(), capabilities=capabilities))

    def _publish(self, event: Event) -> None:
        self._event_publisher.publish(event)
