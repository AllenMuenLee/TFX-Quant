from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tfx_quant.packaging import migrate


def _make_db(path: Path, *, user_version: int = 0, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t(v TEXT)")
    connection.executemany("INSERT INTO t VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    connection.execute(f"PRAGMA user_version = {user_version}")
    connection.commit()
    connection.close()


def _data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "tfx_quant"
    _make_db(data / "orders.sqlite3")
    _make_db(data / "market_data.sqlite3")
    _make_db(data / "logs" / "audit.sqlite3")
    return data


def test_discover_databases_finds_root_and_logs(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    found = {p.name for p in migrate.discover_databases(data)}
    assert found == {"orders.sqlite3", "market_data.sqlite3", "audit.sqlite3"}


def test_check_reports_ok_for_healthy_databases(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    report = migrate.check(data)
    assert report.ok
    assert all(db.integrity == "ok" for db in report.databases)


def test_check_flags_a_corrupt_database(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    (data / "orders.sqlite3").write_bytes(b"not a database at all")
    report = migrate.check(data)
    assert not report.ok
    bad = next(db for db in report.databases if db.path.endswith("orders.sqlite3"))
    assert not bad.ok


def test_check_refuses_a_newer_schema(tmp_path: Path) -> None:
    data = tmp_path / "tfx_quant"
    _make_db(data / "orders.sqlite3", user_version=migrate.SUPPORTED_MAX_USER_VERSION + 5)
    report = migrate.check(data)
    assert not report.ok
    assert report.databases[0].newer_than_supported


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    # a WAL sidecar should be copied too
    (data / "orders.sqlite3-wal").write_bytes(b"wal")

    snapshot = migrate.backup_databases(data)
    assert snapshot.name.startswith("pre-upgrade-")
    assert (snapshot / "orders.sqlite3").is_file()
    assert (snapshot / "orders.sqlite3-wal").is_file()
    assert (snapshot / "logs" / "audit.sqlite3").is_file()

    # Corrupt the live db, then roll back.
    (data / "orders.sqlite3").write_bytes(b"broken")
    restored = migrate.restore_latest_backup(data)
    assert restored == snapshot
    connection = sqlite3.connect(data / "orders.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        connection.close()


def test_restore_latest_backup_without_a_snapshot_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        migrate.restore_latest_backup(tmp_path / "tfx_quant")


def test_cli_check_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = _data_dir(tmp_path)
    assert migrate.main(["--check", "--data-dir", str(data)]) == 0

    (data / "market_data.sqlite3").write_bytes(b"broken")
    assert migrate.main(["--check", "--data-dir", str(data)]) == 2
    assert "FAIL" in capsys.readouterr().out


def test_cli_apply_backs_up_then_checks(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    backup_dir = tmp_path / "backups"
    code = migrate.main(["--apply", "--data-dir", str(data), "--backup-dir", str(backup_dir)])
    assert code == 0
    snapshots = list(backup_dir.glob("pre-upgrade-*"))
    assert len(snapshots) == 1
    assert (snapshots[0] / "orders.sqlite3").is_file()


def test_cli_writes_install_log_when_asked(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    log_path = tmp_path / "updater.log"
    migrate.main(["--check", "--data-dir", str(data), "--log", str(log_path)])
    text = log_path.read_text(encoding="utf-8")
    assert "database_checked" in text
    assert "run_finished" in text
