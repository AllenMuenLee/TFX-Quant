"""Persistence boundary for the append-only execution ledger."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Protocol

from tfx_quant.domain.trade_report import LedgerFill


class FillAppendOutcome(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"


class FillLedgerRepository(Protocol):
    def append(self, fill: LedgerFill) -> FillAppendOutcome: ...
    def list_between(self, start: date, end: date) -> Sequence[LedgerFill]: ...
    def count(self) -> int: ...
