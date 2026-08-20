"""Structured, correlation-aware logging shared across every layer.

This is deliberately a thin, dependency-free foundation (stdlib `logging` +
`contextvars` only) — not the full Feature 13 (rolling files, DB audit table,
retention, diagnostic mode, UI log browser). It exists so Features 01-05 can meet
their own "除錯日誌需求" sections now; Feature 13 is expected to extend the sink
side (handlers/formatters) without changing this call-site API.

Not on any `import-linter` forbidden-modules list for `domain`, `application`,
`infrastructure`, or `persistence`, so every layer may import from here directly.
"""

from tfx_quant.telemetry.events import (
    correlation_scope,
    current_correlation_id,
    current_workflow_id,
    get_logger,
    log_critical,
    log_debug,
    log_error,
    log_event,
    log_info,
    log_warning,
    new_correlation_id,
)
from tfx_quant.telemetry.masking import field_present, mask_account

__all__ = [
    "correlation_scope",
    "current_correlation_id",
    "current_workflow_id",
    "field_present",
    "get_logger",
    "log_critical",
    "log_debug",
    "log_error",
    "log_event",
    "log_info",
    "log_warning",
    "mask_account",
    "new_correlation_id",
]
