"""Process-wide logging configuration for the desktop app's entry point.

Deliberately minimal — a single rotating file plus stderr, both at the process
root logger. Feature 13 owns the real sink story (structured DB audit table,
retention policy, capacity caps, diagnostic-mode expiry); this just makes the
structured events every other feature now emits land somewhere durable in the
meantime, instead of vanishing when nothing has configured a handler.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 5
_LEVEL_ENV_VAR = "TFX_QUANT_LOG_LEVEL"


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

    level = _resolve_level()
    root.setLevel(level)
    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler._tfx_quant_managed = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "tfx_quant.log",
        maxBytes=_DEFAULT_MAX_BYTES,
        backupCount=_DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._tfx_quant_managed = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
