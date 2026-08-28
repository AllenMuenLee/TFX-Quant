"""Persistence layer: SQLite storage.

May depend on `domain` and `application`. Must never depend on `infrastructure` or
`desktop` — enforced by import-linter. Feature 01 only wires the connection factory;
schema/repositories land in Feature 14.
"""
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository

__all__ = ["SqliteFillLedgerRepository"]
