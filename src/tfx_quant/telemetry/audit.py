"""SQLite audit sink, failure-triggered safety pause, and reversal-chain export."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from collections.abc import Callable
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY,
    ts_utc TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    event TEXT NOT NULL,
    correlation_id TEXT,
    workflow_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_workflow_seq ON audit_events(workflow_id, seq);
"""


class SqliteAuditHandler(logging.Handler):
    """Persist structured records; an audit write failure invokes safe pause once."""

    def __init__(self, path: Path, on_critical_failure: Callable[[Exception], None]) -> None:
        super().__init__()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._lock = threading.Lock()
        self._on_critical_failure = on_critical_failure
        self._handling_failure = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(record.getMessage())
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict) or "seq" not in payload or "event" not in payload:
            return
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT OR IGNORE INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        payload["seq"], payload["ts_utc"], record.levelname, record.name,
                        payload["event"], payload.get("correlation_id"),
                        payload.get("workflow_id"), json.dumps(payload, ensure_ascii=False),
                    ),
                )
                self._connection.commit()
        except Exception as exc:
            if bool(payload.get("audit")):
                self._critical_failure(exc)
            else:
                self._fallback_warning(exc)

    def _critical_failure(self, exc: Exception) -> None:
        if self._handling_failure:
            return
        self._handling_failure = True
        try:
            self._fallback_warning(exc, critical=True)
            self._on_critical_failure(exc)
        finally:
            self._handling_failure = False

    @staticmethod
    def _fallback_warning(exc: Exception, *, critical: bool = False) -> None:
        label = "CRITICAL AUDIT" if critical else "audit sink"
        sys.stderr.write(f"{label} persistence failed: {type(exc).__name__}: {exc}\n")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
        super().close()


def install_audit_handler(
    path: Path, *, on_critical_failure: Callable[[Exception], None]
) -> SqliteAuditHandler:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tfx_quant_audit", False):
            root.removeHandler(handler)
            handler.close()
    handler = SqliteAuditHandler(path, on_critical_failure)
    handler._tfx_quant_audit = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    return handler


def export_reversal_chain(audit_path: Path, workflow_id: str, destination: Path) -> int:
    """Export one workflow in global sequence order as UTF-8 JSON Lines."""
    connection = sqlite3.connect(audit_path)
    try:
        rows = connection.execute(
            "SELECT payload_json FROM audit_events WHERE workflow_id = ? ORDER BY seq ASC",
            (workflow_id,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise LookupError(f"no audit events found for reversal workflow {workflow_id}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for (payload_json,) in rows:
            stream.write(str(payload_json))
            stream.write("\n")
    return len(rows)


__all__ = ["SqliteAuditHandler", "export_reversal_chain", "install_audit_handler"]
