"""ReversalWorkflowRepository — the controlled persistence port for reversal workflows.

`persistence.sqlite_reversal_workflow_repository.SqliteReversalWorkflowRepository` is
the real implementation. Mirrors `application.ports.order_repository`'s shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.reversal_workflow import ReversalWorkflowId, ReversalWorkflowRecord


class ReversalWorkflowSaveOutcome(StrEnum):
    INSERTED = "INSERTED"
    """First time this `trigger_key` has been saved."""
    DUPLICATE_KEY = "DUPLICATE_KEY"
    """A workflow already exists for this `trigger_key` — an ordinary outcome, not an
    error. The caller must fetch and reuse the existing record rather than starting a
    second workflow — this is the DB-level half of "不得因後續 K 棒重送"."""


class ReversalWorkflowRepositoryError(Exception):
    """Raised when a write or query could not be completed (I/O failure, locked
    database, etc.) — distinct from `ReversalWorkflowSaveOutcome.DUPLICATE_KEY`, which
    is an ordinary, expected outcome, never a failure."""


class ReversalWorkflowRepository(Protocol):
    def save(self, record: ReversalWorkflowRecord) -> ReversalWorkflowSaveOutcome:
        """Atomic first-time persist of one reversal workflow, keyed by
        `record.trigger_key`."""
        ...

    def update(self, record: ReversalWorkflowRecord) -> None:
        """Persists an already-existing workflow's new state after a transition. Raises
        `ReversalWorkflowRepositoryError` if no row exists for `record.workflow_id`."""
        ...

    def find_by_workflow_id(
        self, workflow_id: ReversalWorkflowId
    ) -> ReversalWorkflowRecord | None: ...

    def find_by_trigger_key(self, trigger_key: str) -> ReversalWorkflowRecord | None: ...

    def find_active_for_contract(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> Sequence[ReversalWorkflowRecord]:
        """Every non-terminal workflow (`ReversalWorkflowRecord.is_active`) for this
        account/contract — `PAUSED_SAFE` counts as active, so an unresolved reversal
        keeps blocking a new one from starting."""
        ...

    def list_active(self) -> Sequence[ReversalWorkflowRecord]:
        """Every non-terminal workflow, any account/contract — the startup/reconnect
        recovery sweep's input set."""
        ...


__all__ = [
    "ReversalWorkflowRepository",
    "ReversalWorkflowRepositoryError",
    "ReversalWorkflowSaveOutcome",
]
