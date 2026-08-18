"""Instrument-selection exceptions — application-layer, not `DomainError` subclasses
(these are about *when* an otherwise-valid domain operation is allowed, not about
malformed domain values). Every message is meant to be shown to the operator as-is.
"""

from __future__ import annotations


class InstrumentSelectionError(Exception):
    """Base class for all `application.instrument_selection` failures."""


class InstrumentMasterEntryNotFoundError(InstrumentSelectionError):
    """No controlled 商品主檔 entry exists for the requested (instrument, contract) —
    "主檔缺漏" in the implementation prompt's wording."""


class SwitchBlockedError(InstrumentSelectionError):
    """`InstrumentSelectionService.switch_to()` was called while switching is not
    currently allowed (strategy executing, open position, active/unknown orders, or a
    post-requery state change) — see `check_switch_allowed()`."""
