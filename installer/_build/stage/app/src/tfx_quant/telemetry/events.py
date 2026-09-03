"""Structured event logging: one JSON object per log record.

Every call site logs an `event` name plus arbitrary structured `fields` rather than a
free-text message. `log_event` stamps each record with a process-wide monotonic
sequence number (so records can be re-ordered after interleaving from multiple
threads/sinks) and both UTC and Asia/Taipei timestamps (Feature 04's "時間同時保留
UTC 與 Asia/Taipei" requirement generalizes to every event, not just market data).

Correlation/workflow IDs thread a single logical operation (a login attempt, an
instrument switch, a bar-close decision) through multiple log records without every
function signature having to carry an explicit ID parameter: `correlation_scope` binds
one for the duration of a `with` block (and everything it calls, on the same thread),
and `log_event` falls back to the bound value when the caller doesn't pass one
explicitly.

Callers choose what belongs in `fields`. Nothing here inspects or redacts field values
— see `telemetry.masking` for the mask/presence-only helpers every call site touching
credentials, account numbers, or other sensitive data must use *before* passing a value
in.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from tfx_quant.telemetry.diagnostics import diagnostic_mode

_TAIPEI = timezone(timedelta(hours=8))

_sequence_lock = threading.Lock()
_sequence_counter = itertools.count(1)

_correlation_id: ContextVar[str | None] = ContextVar("tfx_correlation_id", default=None)
_workflow_id: ContextVar[str | None] = ContextVar("tfx_workflow_id", default=None)


def _next_sequence() -> int:
    with _sequence_lock:
        return next(_sequence_counter)


def new_correlation_id() -> str:
    """A fresh opaque ID for `correlation_scope`/`log_event` — no meaning beyond
    "same value = same logical operation"."""
    return uuid.uuid4().hex


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def current_workflow_id() -> str | None:
    return _workflow_id.get()


@contextmanager
def correlation_scope(
    *, correlation_id: str | None = None, workflow_id: str | None = None
) -> Iterator[None]:
    """Binds `correlation_id`/`workflow_id` for the duration of the block so nested
    `log_event` calls on this thread pick them up without threading an explicit
    parameter through every function. Restores whatever was bound before on exit
    (nesting is supported). A `None` argument leaves that ID unchanged rather than
    clearing it."""
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    if correlation_id is not None:
        tokens.append((_correlation_id, _correlation_id.set(correlation_id)))
    if workflow_id is not None:
        tokens.append((_workflow_id, _workflow_id.set(workflow_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    correlation_id: str | None = None,
    workflow_id: str | None = None,
    audit: bool = False,
    **fields: Any,
) -> None:
    """Emits one structured record: a JSON object (event name, sequence number, UTC +
    Taipei timestamps, correlation/workflow IDs, and every `fields` entry) as the log
    message, at `level`, on `logger`.

    `fields` values are passed through `json.dumps(..., default=str)`, so domain
    value objects (`Timestamp`, `Decimal`, enums, dataclasses without a custom
    encoder) degrade to their `str()` rather than raising — structured logging must
    never itself crash the caller. Callers are responsible for masking anything
    sensitive before it reaches `fields` (see `telemetry.masking`).

    No-ops without formatting the payload when `logger` is not enabled for `level`,
    so a DEBUG-heavy call site (e.g. per-tick diagnostics) costs nothing at INFO.
    """
    resolved_workflow_id = workflow_id if workflow_id is not None else _workflow_id.get()
    diagnostic_override = level == logging.DEBUG and diagnostic_mode.allows(
        workflow_id=resolved_workflow_id,
        order_id=_order_id_from(fields),
    )
    if not logger.isEnabledFor(level) and not diagnostic_override:
        return
    now_utc = datetime.now(UTC)
    payload: dict[str, Any] = {
        "event": event,
        "seq": _next_sequence(),
        "ts_utc": now_utc.isoformat(),
        "ts_taipei": now_utc.astimezone(_TAIPEI).isoformat(),
        "correlation_id": correlation_id if correlation_id is not None else _correlation_id.get(),
        "workflow_id": resolved_workflow_id,
        "audit": audit,
        **fields,
    }
    message = json.dumps(payload, default=str, ensure_ascii=False)
    if diagnostic_override and not logger.isEnabledFor(level):
        # Logger._log creates and handles a DEBUG LogRecord without changing the
        # process-wide level. This limits elevated detail to the selected target.
        logger._log(level, message, ())
    else:
        logger.log(level, message)


def _order_id_from(fields: dict[str, Any]) -> str | None:
    for name in ("order_id", "client_order_id", "local_order_id", "broker_order_no"):
        value = fields.get(name)
        if value is not None:
            return str(value)
    return None


def log_debug(logger: logging.Logger, event: str, *, audit: bool = False, **fields: Any) -> None:
    log_event(logger, logging.DEBUG, event, audit=audit, **fields)


def log_info(logger: logging.Logger, event: str, *, audit: bool = False, **fields: Any) -> None:
    log_event(logger, logging.INFO, event, audit=audit, **fields)


def log_warning(logger: logging.Logger, event: str, *, audit: bool = False, **fields: Any) -> None:
    log_event(logger, logging.WARNING, event, audit=audit, **fields)


def log_error(logger: logging.Logger, event: str, *, audit: bool = False, **fields: Any) -> None:
    log_event(logger, logging.ERROR, event, audit=audit, **fields)


def log_critical(logger: logging.Logger, event: str, *, audit: bool = False, **fields: Any) -> None:
    log_event(logger, logging.CRITICAL, event, audit=audit, **fields)
