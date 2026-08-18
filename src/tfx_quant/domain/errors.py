"""Domain-specific exceptions.

All illegal-state rejections in the domain layer raise one of these (never a bare
ValueError), so callers can catch `DomainError` to route into a safe-pause path.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-layer validation errors."""


class InvalidQuantityError(DomainError):
    """Raised when an order or position quantity violates the lot-count rules."""


class InvalidMoneyError(DomainError):
    """Raised when a Price/Money value is constructed from a non-Decimal or illegal amount."""


class InvalidInstrumentError(DomainError):
    """Raised when an instrument code is not one of the supported futures products."""


class InvalidContractMonthError(DomainError):
    """Raised when a contract month is out of range or malformed."""


class InvalidTimestampError(DomainError):
    """Raised when a timestamp is naive or not in the Asia/Taipei time zone."""


class InvalidAccountError(DomainError):
    """Raised when trading account fields are empty or malformed."""


class IllegalStateTransitionError(DomainError):
    """Raised when a StrategyStateMachine transition is not in the legal transition table."""


class InvalidPositionError(DomainError):
    """Raised when a Position's fields are internally inconsistent."""


class InvalidOrderError(DomainError):
    """Raised when an Order's fields are internally inconsistent."""


class InvalidInstrumentMasterEntryError(DomainError):
    """Raised when a 商品主檔 (instrument master) entry's fields are internally
    inconsistent (blank vendor symbol, non-positive tick size/multiplier, malformed
    trading session)."""


class InvalidTickError(DomainError):
    """Raised when a market-data tick's fields are internally inconsistent (negative
    size/cumulative volume) — construction-time validation, separate from the
    aggregator's own duplicate/out-of-order dedup policy."""
