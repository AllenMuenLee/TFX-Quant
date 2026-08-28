from __future__ import annotations

import json
import logging
from pathlib import Path

from tfx_quant.telemetry import log_info
from tfx_quant.telemetry.audit import SqliteAuditHandler, export_reversal_chain


def _audit_logger(handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger("test.audit")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


def test_critical_audit_failure_invokes_safe_pause_callback_once(tmp_path: Path) -> None:
    failures: list[Exception] = []
    handler = SqliteAuditHandler(tmp_path / "audit.sqlite3", failures.append)
    logger = _audit_logger(handler)
    handler._connection.close()  # simulate a sink that became unavailable after readiness

    log_info(logger, "reversal_workflow_started", audit=True, workflow_id="wf-1")

    assert len(failures) == 1


def test_noncritical_audit_failure_does_not_pause(tmp_path: Path) -> None:
    failures: list[Exception] = []
    handler = SqliteAuditHandler(tmp_path / "audit.sqlite3", failures.append)
    logger = _audit_logger(handler)
    handler._connection.close()

    log_info(logger, "ordinary_diagnostic")

    assert failures == []


def test_export_reversal_chain_is_complete_and_sequence_ordered(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.sqlite3"
    handler = SqliteAuditHandler(audit_path, lambda _exc: None)
    logger = _audit_logger(handler)
    expected = [
        "reversal_workflow_started",
        "reversal_position_queried",
        "reversal_close_order_submitted",
        "order_report_applied",
        "reversal_close_order_filled",
        "reversal_flat_confirmed",
        "reversal_entry_order_submitted",
        "reversal_completed",
    ]
    for event in expected:
        log_info(logger, event, audit=True, workflow_id="wf-1")
    log_info(logger, "different_workflow", audit=True, workflow_id="wf-2")
    handler.close()

    destination = tmp_path / "exports" / "wf-1.jsonl"
    count = export_reversal_chain(audit_path, "wf-1", destination)
    payloads = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]

    assert count == len(expected)
    assert [payload["event"] for payload in payloads] == expected
    assert [payload["seq"] for payload in payloads] == sorted(
        payload["seq"] for payload in payloads
    )
    assert {payload["workflow_id"] for payload in payloads} == {"wf-1"}
