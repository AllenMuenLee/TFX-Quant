"""Order-management exceptions — application-layer, not `DomainError` subclasses (these
are about *when* an otherwise-valid order is allowed to be submitted, not about
malformed domain values). Every message is meant to be shown to the operator as-is.
"""

from __future__ import annotations


class OrderManagementError(Exception):
    """Base class for all `application.order_management` failures."""


class ActiveWorkflowInProgressError(OrderManagementError):
    """`OrderManager.submit` was called for an account/instrument/contract that already
    has a non-terminal order intent — "同一時間只允許一個改變持倉的 workflow"."""


class OrderExposureExceededError(OrderManagementError):
    """The worst-case net position this order could produce (current position plus every
    possible fill outcome of every still-active order) would exceed the 2-lot cap."""


class OrderNotFoundError(OrderManagementError):
    """`OrderManager.cancel` was called for a `ClientOrderId` with no matching local
    intent."""


class UnsupportedTradeInstrumentError(OrderManagementError):
    """An order attempted to trade anything other than Mini-TAIEX futures (MXF)."""
