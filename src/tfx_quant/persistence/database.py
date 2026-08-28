"""Versioned SQLite lifecycle with integrity checks and pre-migration backups."""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_logger = logging.getLogger(__name__)
LATEST_SCHEMA_VERSION = 1

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    configuration_version TEXT NOT NULL,
    migrated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_snapshots (
    snapshot_id TEXT PRIMARY KEY, bar_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pnl_ledger (
    entry_id TEXT PRIMARY KEY, workflow_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, correlation_id TEXT,
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_reports (
    recovery_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DatabaseStartupError(RuntimeError):
    """The database cannot safely be used; callers must remain paused."""


@dataclass(frozen=True, slots=True)
class DatabaseStartupResult:
    connection: sqlite3.Connection
    schema_version: int
    integrity_result: str
    backup_id: str | None


def _masked(path: Path) -> str:
    # Keep only a stable fingerprint and filename; never expose user/profile directories.
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:10]
    return f"{path.name}#{digest}"


def open_managed_database(
    path: Path, *, configuration_version: str, target_version: int = LATEST_SCHEMA_VERSION
) -> DatabaseStartupResult:
    """Open, verify and migrate a database, backing up every existing DB first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    backup_id: str | None = None
    started = time.monotonic()
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise DatabaseStartupError(f"SQLite integrity check failed: {integrity}")
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > target_version:
            raise DatabaseStartupError(
                f"database schema {current} is newer than supported {target_version}"
            )
        if current < target_version:
            if existed and path.stat().st_size:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                backup = path.with_name(f"{path.name}.backup-{stamp}")
                destination = sqlite3.connect(backup)
                try:
                    connection.backup(destination)
                finally:
                    destination.close()
                backup_id = backup.name
            _migrate(connection, current, target_version, configuration_version)
        _logger.info(
            "database_startup_completed path=%s schema_version=%d integrity=%s "
            "backup_id=%s duration_ms=%.3f",
            _masked(path), target_version, integrity, backup_id,
            (time.monotonic() - started) * 1000,
        )
        return DatabaseStartupResult(connection, target_version, integrity, backup_id)
    except Exception:
        connection.close()
        _logger.exception("database_startup_failed path=%s", _masked(path))
        raise


def _migrate(
    connection: sqlite3.Connection, current: int, target: int, configuration_version: str
) -> None:
    migrations = {1: _MIGRATION_1}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for version in range(current + 1, target + 1):
            script = migrations.get(version)
            if script is None:
                raise DatabaseStartupError(f"missing migration {version}")
            # executescript commits implicitly, so execute statements individually to retain
            # one rollback boundary for the entire upgrade.
            for statement in script.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")
        connection.execute(
            "INSERT OR REPLACE INTO schema_metadata VALUES (1, ?, ?, ?)",
            (target, configuration_version, datetime.now(UTC).isoformat()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def restore_backup(backup: Path, destination: Path) -> None:
    """Restore a verified backup while retaining the failed database beside it."""
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        result = str(source.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise DatabaseStartupError(f"backup integrity check failed: {result}")
    finally:
        source.close()
    shutil.copy2(backup, destination)
