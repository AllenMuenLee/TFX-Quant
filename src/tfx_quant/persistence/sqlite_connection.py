"""SQLite connection factory.

Minimal on purpose — Feature 01 only needs the `persistence` package to exist with
the right dependency direction (domain/application only). Schema, migrations, and
repositories are Feature 14's job (persistence and recovery).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def create_connection(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) a SQLite database at `db_path`.

    Foreign keys are enabled explicitly — SQLite defaults them off per-connection.
    `check_same_thread=False` is for callers (e.g. `SqliteBarRecordRepository`) that
    serialize their own access with a lock across a dedicated writer thread plus other
    reader threads — sqlite3's own default same-thread check would otherwise reject
    that usage outright.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
