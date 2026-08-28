"""Process-wide logging configuration for the desktop app's entry point.

Installs stderr plus the bounded, human-readable buffer used by the desktop log
viewer. The SQLite audit handler is installed separately after service construction,
because its critical-write failure callback needs the strategy state machine.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LEVEL_ENV_VAR = "TFX_QUANT_LOG_LEVEL"
_LOG_CAPACITY = 10_000
_log_lines: deque[str] = deque(maxlen=_LOG_CAPACITY)
_log_lock = threading.Lock()


class _HumanReadableFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            detail = message
        else:
            event = str(payload.pop("event", "log"))
            payload.pop("ts_utc", None)
            payload.pop("ts_taipei", None)
            fields = [f"{key}={value}" for key, value in payload.items() if value is not None]
            detail = event + (" | " + " | ".join(fields) if fields else "")
        return f"{self.formatTime(record)} {record.levelname:<8} {record.name} - {detail}"


class _MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        with _log_lock:
            _log_lines.append(line)


def get_log_lines() -> tuple[str, ...]:
    """Return a stable snapshot for the desktop log viewer."""
    with _log_lock:
        return tuple(_log_lines)


def _resolve_level() -> int:
    name = os.environ.get(_LEVEL_ENV_VAR, "INFO").strip().upper()
    return logging.getLevelNamesMapping().get(name, logging.INFO)


def configure_logging(log_dir: Path) -> None:
    """Idempotent: safe to call more than once (e.g. from tests) — clears any handlers
    this function previously installed on the root logger before re-installing."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tfx_quant_managed", False):
            root.removeHandler(handler)
    with _log_lock:
        _log_lines.clear()

    level = _resolve_level()
    root.setLevel(level)
    formatter = _HumanReadableFormatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler._tfx_quant_managed = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)
    memory_handler = _MemoryLogHandler()
    memory_handler.setFormatter(formatter)
    memory_handler._tfx_quant_managed = True  # type: ignore[attr-defined]
    root.addHandler(memory_handler)
    # Intentionally terminal-only. The desktop contains no log console and this
    # launcher no longer creates or appends a log file behind the user's back.
    _ = log_dir  # kept in the public signature for compatibility with launchers/tests
