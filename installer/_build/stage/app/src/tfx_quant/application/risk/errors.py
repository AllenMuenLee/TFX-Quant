"""Risk-supervisor exceptions — application-layer, not `DomainError` subclasses (these
are about *when* an otherwise-valid flatten workflow is allowed to start, not about
malformed domain values). Every message is meant to be shown to the operator as-is.
"""

from __future__ import annotations


class RiskSupervisorError(Exception):
    """Base class for all `application.risk` failures."""


class EodFlattenAlreadyActiveError(RiskSupervisorError):
    """A flatten trigger (scheduled or emergency) was requested for an account/
    instrument/contract that already has a non-terminal flatten workflow (including
    `PAUSED_SAFE`) — only one flatten workflow may be in flight per contract."""


class StaleEmergencyConfirmationError(RiskSupervisorError):
    """`RiskSupervisor.trigger_emergency_flatten`'s caller-confirmed lot count no
    longer matches a fresh broker position query — the operator must re-confirm against
    the current position before the emergency flatten can proceed. Mirrors
    `application.position_reconciliation.errors.StaleSyncConfirmationError`'s "never
    trust the caller's confirmed number alone" posture."""


__all__ = [
    "EodFlattenAlreadyActiveError",
    "RiskSupervisorError",
    "StaleEmergencyConfirmationError",
]
