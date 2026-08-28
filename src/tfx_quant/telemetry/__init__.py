"""Structured, correlation-aware logging shared across every layer.

The call-site API stays deliberately small. Sink-side modules add the bounded UI
buffer, SQLite audit persistence, failure-triggered safe pause, bounded diagnostic
elevation, and reversal-chain export without coupling domain code to desktop code.

Not on any `import-linter` forbidden-modules list for `domain`, `application`,
`infrastructure`, or `persistence`, so every layer may import from here directly.
"""

from tfx_quant.telemetry.diagnostics import DiagnosticMode, DiagnosticStatus, diagnostic_mode
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
    "DiagnosticMode",
    "DiagnosticStatus",
    "diagnostic_mode",
]
