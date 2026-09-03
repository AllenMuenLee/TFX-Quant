"""PositionBaselineRepository — the controlled persistence port for `domain.
position_reconciliation.PositionBaseline`.

`persistence.sqlite_position_baseline_repository.SqlitePositionBaselineRepository` is
the real implementation. Unlike `OrderRepository`/`ReversalWorkflowRepository` there is
no idempotency/trigger-key dedup concept here — a baseline is naturally one row per
(account, instrument, contract), always safe to upsert.
"""

from __future__ import annotations

from typing import Protocol

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.position_reconciliation import PositionBaseline


class PositionBaselineRepositoryError(Exception):
    """Raised when a write or query could not be completed (I/O failure, locked
    database, etc.)."""


class PositionBaselineRepository(Protocol):
    def get(
        self, account: TradingAccount, instrument: Instrument, contract: ContractMonth
    ) -> PositionBaseline | None:
        """`None` when no baseline has ever been recorded for this (account,
        instrument, contract) — callers treat that as an assumed-flat 0 lots."""
        ...

    def upsert(self, baseline: PositionBaseline) -> None:
        """Insert-or-replace the single row for `(baseline.account, baseline.
        instrument, baseline.contract)`."""
        ...


__all__ = ["PositionBaselineRepository", "PositionBaselineRepositoryError"]
