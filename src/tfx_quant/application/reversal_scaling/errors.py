"""Reversal/scaling exceptions — application-layer, not `DomainError` subclasses (these
are about *when* an otherwise-valid workflow/order is allowed to start, not about
malformed domain values). Every message is meant to be shown to the operator as-is.
"""

from __future__ import annotations


class ReversalScalingError(Exception):
    """Base class for all `application.reversal_scaling` failures."""


class ReversalAlreadyActiveError(ReversalScalingError):
    """`ReversalWorkflowService.start_reversal` was called for an account/instrument/
    contract that already has a non-terminal reversal workflow (including
    `PAUSED_SAFE`) — only one reversal workflow may be in flight per contract."""


class InvalidSignalKindError(ReversalScalingError):
    """A `StrategySignal` of the wrong `SignalKind` was passed to a service that only
    handles a specific kind (e.g. an `ADD_LONG` signal passed to
    `ReversalWorkflowService.start_reversal`, which only handles `REVERSE`)."""
