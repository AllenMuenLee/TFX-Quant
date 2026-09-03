"""Pre-upgrade data-safety step for the installer / updater.

The prompt requires: *升級前停止程式、備份資料庫、驗證 migration；失敗時保留可復原
版本與清楚訊息。*

This project's per-feature SQLite databases (``orders.sqlite3``,
``market_data.sqlite3``, ``fill_ledger.sqlite3``, …) each create their own schema
idempotently with ``CREATE TABLE IF NOT EXISTS`` on the next launch, so there is no
destructive migration script to run here. What this step does — and what the
installer calls before replacing program files — is:

1. **integrity-check** every database under the per-user data directory;
2. **refuse the upgrade** (non-zero exit, nothing touched) if any database is
   corrupt or was written by a newer build than this one supports
   (``PRAGMA user_version`` above :data:`SUPPORTED_MAX_USER_VERSION`);
3. otherwise **copy every database aside** (with its ``-wal`` / ``-shm`` sidecars)
   into ``backup\\pre-upgrade-<timestamp>\\`` so a failed upgrade can be rolled
   back with ``--restore-latest``.

Reuses :func:`tfx_quant.persistence.database.restore_backup` (verified restore) and
:data:`tfx_quant.persistence.database.LATEST_SCHEMA_VERSION`.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from tfx_quant.persistence.database import LATEST_SCHEMA_VERSION, restore_backup

SUPPORTED_MAX_USER_VERSION = LATEST_SCHEMA_VERSION
"""``PRAGMA user_version`` this build understands. The self-migrating repositories
leave it at 0; the managed lifecycle in ``persistence/database.py`` raises it to
``LATEST_SCHEMA_VERSION``. Anything higher was written by a newer app."""

_DB_RELATIVE_GLOBS = ("*.sqlite3", "logs/*.sqlite3")
_SIDECAR_SUFFIXES = ("-wal", "-shm")


def default_data_dir() -> Path:
    import os

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "tfx_quant"


def _timestamp_token() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True, slots=True)
class DatabaseCheck:
    path: str
    integrity: str
    user_version: int
    newer_than_supported: bool

    @property
    def ok(self) -> bool:
        return self.integrity == "ok" and not self.newer_than_supported


@dataclass(frozen=True, slots=True)
class CheckReport:
    data_dir: str
    databases: tuple[DatabaseCheck, ...]

    @property
    def ok(self) -> bool:
        return all(db.ok for db in self.databases)

    def as_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def discover_databases(data_dir: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in _DB_RELATIVE_GLOBS:
        found.extend(sorted(data_dir.glob(pattern)))
    # Deduplicate while keeping order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _check_one(path: Path) -> DatabaseCheck:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return DatabaseCheck(str(path), f"open-failed: {exc}", -1, False)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        return DatabaseCheck(str(path), f"unreadable: {exc}", -1, False)
    finally:
        connection.close()
    return DatabaseCheck(
        path=str(path),
        integrity=integrity,
        user_version=user_version,
        newer_than_supported=user_version > SUPPORTED_MAX_USER_VERSION,
    )


def check(data_dir: Path) -> CheckReport:
    databases = tuple(_check_one(path) for path in discover_databases(data_dir))
    return CheckReport(data_dir=str(data_dir), databases=databases)


def backup_databases(data_dir: Path, backup_root: Path | None = None) -> Path:
    """Copy every database (plus ``-wal`` / ``-shm`` sidecars) into a fresh
    ``pre-upgrade-<timestamp>`` directory. Returns that directory."""
    root = backup_root or (data_dir / "backup")
    destination = root / f"pre-upgrade-{_timestamp_token()}"
    destination.mkdir(parents=True, exist_ok=True)
    for db_path in discover_databases(data_dir):
        rel = db_path.relative_to(data_dir)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, target)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, target.with_name(target.name + suffix))
    return destination


def latest_backup(backup_root: Path) -> Path | None:
    if not backup_root.is_dir():
        return None
    candidates = sorted(
        (p for p in backup_root.iterdir() if p.is_dir() and p.name.startswith("pre-upgrade-")),
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def restore_latest_backup(data_dir: Path, backup_root: Path | None = None) -> Path:
    """Restore the most recent ``pre-upgrade-*`` snapshot over the live data
    directory. Each file's integrity is verified before it is copied back
    (``persistence.database.restore_backup``). Returns the snapshot used."""
    root = backup_root or (data_dir / "backup")
    snapshot = latest_backup(root)
    if snapshot is None:
        raise FileNotFoundError(f"no pre-upgrade backup found under {root}")
    for backup_file in sorted(snapshot.rglob("*.sqlite3")):
        rel = backup_file.relative_to(snapshot)
        destination = data_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A stale live -wal/-shm alongside a restored database file would be replayed
        # by SQLite over the wrong content — drop them, then restore the snapshot's.
        for suffix in _SIDECAR_SUFFIXES:
            live_sidecar = destination.with_name(destination.name + suffix)
            live_sidecar.unlink(missing_ok=True)
        restore_backup(backup_file, destination)
        for suffix in _SIDECAR_SUFFIXES:
            backup_sidecar = backup_file.with_name(backup_file.name + suffix)
            if backup_sidecar.exists():
                shutil.copy2(backup_sidecar, destination.with_name(destination.name + suffix))
    return snapshot


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="tfx_quant.packaging.migrate")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="integrity-check only")
    mode.add_argument("--apply", action="store_true", help="back up, then check")
    mode.add_argument("--restore-latest", action="store_true", help="roll back a failed upgrade")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log", type=Path, default=None, help="append events to this install log")
    args = parser.parse_args(argv)

    data_dir = args.data_dir or default_data_dir()
    log = None
    if args.log is not None:
        from tfx_quant.packaging.install_log import InstallLogger

        log = InstallLogger(
            phase="updater", package_version="n/a", app_version="n/a", log_path=args.log
        )

    try:
        if args.restore_latest:
            snapshot = restore_latest_backup(data_dir, args.backup_dir)
            if log is not None:
                log.event("rollback_restored", snapshot=str(snapshot))
                log.close(exit_code=0, rollback_result="restored")
            print(f"restored from {snapshot}")
            return 0

        if args.apply:
            snapshot = backup_databases(data_dir, args.backup_dir)
            if log is not None:
                log.event(
                    "databases_backed_up",
                    snapshot=str(snapshot),
                    count=len(discover_databases(data_dir)),
                )
            print(f"backed up to {snapshot}")

        report = check(data_dir)
        if args.json:
            print(report.as_json())
        else:
            for db in report.databases:
                flag = "OK" if db.ok else "FAIL"
                print(f"[{flag}] {db.path} integrity={db.integrity} user_version={db.user_version}")
            if not report.databases:
                print("no databases found (fresh install)")
        if log is not None:
            for db in report.databases:
                log.event(
                    "database_checked",
                    path=db.path,
                    integrity=db.integrity,
                    user_version=db.user_version,
                    newer_than_supported=db.newer_than_supported,
                    ok=db.ok,
                )
            log.close(exit_code=0 if report.ok else 1)
        return 0 if report.ok else 2
    except Exception as exc:
        if log is not None:
            log.event("updater_failed", error_type=type(exc).__name__)
            log.close(exit_code=1, rollback_result="not-attempted")
        print(f"error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
