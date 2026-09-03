"""OrderRepository — the controlled persistence port for order intents.

`persistence.sqlite_order_repository.SqliteOrderRepository` is the real implementation.
Mirrors `application.ports.bar_record_repository`'s shape: an outcome enum for the
ordinary "already exists" case (never an exception) and a distinct exception type for
genuine I/O failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import ClientOrderId
from tfx_quant.domain.order_state_machine import LocalOrderId, OrderIntent


class OrderIntentSaveOutcome(StrEnum):
    INSERTED = "INSERTED"
    """First time this `idempotency_key` has been saved."""
    DUPLICATE_KEY = "DUPLICATE_KEY"
    """An intent already exists for this `idempotency_key` — an ordinary outcome, not an
    error. The caller must fetch and reuse the existing record via
    `find_by_idempotency_key` rather than proceeding to call the broker gateway — this is
    the DB-level half of "同一 intent 不可由重試或重複 K 棒再次送出"."""


class OrderRepositoryError(Exception):
    """Raised when a write or query could not be completed (I/O failure, locked
    database, etc.) — distinct from `OrderIntentSaveOutcome.DUPLICATE_KEY`, which is an
    ordinary, expected outcome, never a failure."""


class OrderRepository(Protocol):
    def save_intent(self, intent: OrderIntent) -> OrderIntentSaveOutcome:
        """Atomic first-time persist of one order intent, keyed by
        `intent.idempotency_key`. Must complete before any broker API call is made — see
        `application.order_management.order_manager.OrderManager.submit`."""
        ...

    def update_intent(self, intent: OrderIntent) -> None:
        """Persists an already-existing intent's new state after a transition. Raises
        `OrderRepositoryError` if no row exists for `intent.local_order_id` — every call
        site transitions an intent that was itself loaded from this repository first."""
        ...

    def find_by_local_order_id(self, local_order_id: LocalOrderId) -> OrderIntent | None: ...

    def find_by_client_order_id(self, client_order_id: ClientOrderId) -> OrderIntent | None: ...

    def find_by_idempotency_key(self, idempotency_key: str) -> OrderIntent | None: ...

    def find_active_for_contract(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> Sequence[OrderIntent]:
        """Every non-terminal intent (`OrderIntent.is_active`) for this account/contract —
        `UNKNOWN` counts as active, so an unresolved order keeps blocking new submissions
        rather than silently freeing the slot."""
        ...

    def list_active(self) -> Sequence[OrderIntent]:
        """Every non-terminal intent, any account/contract — the startup/reconnect
        reconciliation sweep's input set."""
        ...

    def list_all(self) -> Sequence[OrderIntent]:
        """Every intent ever persisted, oldest first — the read-only order blotter the
        desktop UI renders. Terminal (`FILLED`/`CANCELLED`/`REJECTED`) intents included,
        unlike `list_active`."""
        ...


__all__ = ["OrderIntentSaveOutcome", "OrderRepository", "OrderRepositoryError"]
