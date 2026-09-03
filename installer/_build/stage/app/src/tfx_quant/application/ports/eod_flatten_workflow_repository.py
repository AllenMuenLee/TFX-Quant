"""EodFlattenWorkflowRepository — the controlled persistence port for Feature 10's
04:55/emergency flatten workflows.

`persistence.sqlite_eod_flatten_workflow_repository.SqliteEodFlattenWorkflowRepository`
is the real implementation. Mirrors `application.ports.reversal_workflow_repository`'s
shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.risk import EodFlattenWorkflowId, EodFlattenWorkflowRecord


class EodFlattenWorkflowSaveOutcome(StrEnum):
    INSERTED = "INSERTED"
    """First time this `trigger_key` has been saved."""
    DUPLICATE_KEY = "DUPLICATE_KEY"
    """A workflow already exists for this `trigger_key` — an ordinary outcome, not an
    error. The caller must fetch and reuse the existing record rather than starting a
    second workflow — this is the DB-level half of "同一交易日不得重複觸發 04:55 平倉"."""


class EodFlattenWorkflowRepositoryError(Exception):
    """Raised when a write or query could not be completed (I/O failure, locked
    database, etc.) — distinct from `EodFlattenWorkflowSaveOutcome.DUPLICATE_KEY`, which
    is an ordinary, expected outcome, never a failure."""


class EodFlattenWorkflowRepository(Protocol):
    def save(self, record: EodFlattenWorkflowRecord) -> EodFlattenWorkflowSaveOutcome:
        """Atomic first-time persist of one flatten workflow, keyed by
        `record.trigger_key`."""
        ...

    def update(self, record: EodFlattenWorkflowRecord) -> None:
        """Persists an already-existing workflow's new state after a transition. Raises
        `EodFlattenWorkflowRepositoryError` if no row exists for `record.workflow_id`."""
        ...

    def find_by_workflow_id(
        self, workflow_id: EodFlattenWorkflowId
    ) -> EodFlattenWorkflowRecord | None: ...

    def find_by_trigger_key(self, trigger_key: str) -> EodFlattenWorkflowRecord | None: ...

    def find_active_for_contract(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> Sequence[EodFlattenWorkflowRecord]:
        """Every non-terminal workflow (`EodFlattenWorkflowRecord.is_active`) for this
        account/contract — `PAUSED_SAFE` counts as active, so an unresolved flatten keeps
        blocking a new one from starting."""
        ...

    def list_active(self) -> Sequence[EodFlattenWorkflowRecord]:
        """Every non-terminal workflow, any account/contract — the startup/reconnect
        recovery sweep's input set."""
        ...


__all__ = [
    "EodFlattenWorkflowRepository",
    "EodFlattenWorkflowRepositoryError",
    "EodFlattenWorkflowSaveOutcome",
]
