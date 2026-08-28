from __future__ import annotations

from datetime import timedelta

from tfx_quant.telemetry.diagnostics import DiagnosticMode


def test_diagnostic_mode_expires() -> None:
    now = [10.0]
    mode = DiagnosticMode(monotonic=lambda: now[0])
    mode.enable(workflow_id="wf-1", duration=timedelta(seconds=5), max_events=2)

    assert mode.allows(workflow_id="wf-1", order_id=None)
    now[0] = 16.0

    assert not mode.allows(workflow_id="wf-1", order_id=None)
    assert mode.status() is None


def test_diagnostic_mode_enforces_capacity_and_target() -> None:
    mode = DiagnosticMode(monotonic=lambda: 10.0)
    mode.enable(order_id="order-1", duration=timedelta(minutes=1), max_events=2)

    assert not mode.allows(workflow_id=None, order_id="other")
    assert mode.allows(workflow_id=None, order_id="order-1")
    assert mode.allows(workflow_id=None, order_id="order-1")
    assert not mode.allows(workflow_id=None, order_id="order-1")
    assert mode.status() is None


def test_diagnostic_mode_requires_one_target_and_positive_limits() -> None:
    mode = DiagnosticMode()

    for kwargs in (
        {},
        {"workflow_id": "wf", "order_id": "order"},
        {"workflow_id": "wf", "max_events": 0},
        {"workflow_id": "wf", "duration": timedelta(0)},
    ):
        try:
            mode.enable(**kwargs)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError("invalid diagnostic configuration was accepted")
