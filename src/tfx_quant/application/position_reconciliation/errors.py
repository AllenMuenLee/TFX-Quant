"""Position-reconciliation exceptions — application-layer, not `DomainError`
subclasses (these are about *when* a manual sync is allowed, not about malformed
domain values). Every message is meant to be shown to the operator as-is.
"""

from __future__ import annotations


class PositionReconciliationError(Exception):
    """Base class for all `application.position_reconciliation` failures."""


class ManualSyncBlockedError(PositionReconciliationError):
    """`PositionReconciliationService.confirm_manual_sync` was called while an active
    or unknown-status order still exists for the contract — "同步前必須確認無活動或未知
    委託；有未知委託時禁止同步，先由人工在券商端釐清"."""


class StaleSyncConfirmationError(PositionReconciliationError):
    """The operator's confirmed actual-position/snapshot-time no longer matches a fresh
    broker query — the broker position moved again between "重新查詢" and "確認同步", so
    the confirmation is rejected rather than silently accepted against stale data. The
    operator must requery and confirm again."""
