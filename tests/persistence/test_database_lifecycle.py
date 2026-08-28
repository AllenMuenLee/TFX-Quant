from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tfx_quant.persistence.database import DatabaseStartupError, open_managed_database


def test_new_database_is_integrity_checked_and_migrated(tmp_path: Path) -> None:
    result = open_managed_database(tmp_path / "state.db", configuration_version="cfg-7")
    try:
        assert result.integrity_result == "ok"
        assert result.schema_version == 1
        assert result.backup_id is None
        assert result.connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert result.connection.execute(
            "SELECT configuration_version FROM schema_metadata"
        ).fetchone() == ("cfg-7",)
    finally:
        result.connection.close()


def test_existing_database_is_backed_up_before_migration(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.execute("INSERT INTO legacy VALUES ('preserved')")
    connection.commit()
    connection.close()

    result = open_managed_database(path, configuration_version="cfg-1")
    result.connection.close()
    assert result.backup_id is not None
    backup = sqlite3.connect(tmp_path / result.backup_id)
    try:
        assert backup.execute("SELECT value FROM legacy").fetchone() == ("preserved",)
        assert backup.execute("PRAGMA user_version").fetchone() == (0,)
    finally:
        backup.close()


def test_corrupt_database_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises((DatabaseStartupError, sqlite3.DatabaseError)):
        open_managed_database(path, configuration_version="cfg-1")
