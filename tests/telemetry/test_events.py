from __future__ import annotations

import json
import logging

import pytest

from tfx_quant.telemetry import correlation_scope, log_event, log_info, new_correlation_id


@pytest.fixture
def logger(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    logger = logging.getLogger("tfx_quant.telemetry.tests")
    logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="tfx_quant.telemetry.tests")
    return logger


def _payload(caplog: pytest.LogCaptureFixture, index: int = -1) -> dict:
    return json.loads(caplog.records[index].message)


def test_log_event_emits_json_payload_with_core_fields(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    log_event(logger, logging.INFO, "widget_built", widget_id="w-1")

    payload = _payload(caplog)
    assert payload["event"] == "widget_built"
    assert payload["widget_id"] == "w-1"
    assert isinstance(payload["seq"], int)
    assert payload["ts_utc"].endswith("+00:00")
    assert payload["ts_taipei"].endswith("+08:00")
    assert payload["correlation_id"] is None
    assert payload["workflow_id"] is None


def test_sequence_numbers_strictly_increase_across_calls(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    log_info(logger, "a")
    log_info(logger, "b")

    seq_a = _payload(caplog, -2)["seq"]
    seq_b = _payload(caplog, -1)["seq"]
    assert seq_b > seq_a


def test_correlation_scope_binds_ids_for_nested_log_calls(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    correlation_id = new_correlation_id()
    with correlation_scope(correlation_id=correlation_id, workflow_id="wf-1"):
        log_info(logger, "inside_scope")
    log_info(logger, "outside_scope")

    inside = _payload(caplog, -2)
    outside = _payload(caplog, -1)
    assert inside["correlation_id"] == correlation_id
    assert inside["workflow_id"] == "wf-1"
    assert outside["correlation_id"] is None
    assert outside["workflow_id"] is None


def test_explicit_correlation_id_overrides_bound_scope(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with correlation_scope(correlation_id="bound"):
        log_event(logger, logging.INFO, "explicit_wins", correlation_id="explicit")

    assert _payload(caplog)["correlation_id"] == "explicit"


def test_correlation_scope_restores_previous_value_on_nested_exit(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with correlation_scope(correlation_id="outer"):
        with correlation_scope(correlation_id="inner"):
            log_info(logger, "inner_event")
        log_info(logger, "outer_event_again")

    inner = _payload(caplog, -2)
    outer_again = _payload(caplog, -1)
    assert inner["correlation_id"] == "inner"
    assert outer_again["correlation_id"] == "outer"


def test_disabled_level_does_not_emit(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("tfx_quant.telemetry.tests.disabled")
    caplog.set_level(logging.DEBUG, logger="tfx_quant.telemetry.tests.disabled")
    logger.setLevel(logging.WARNING)

    log_info(logger, "should_not_appear")

    assert len(caplog.records) == 0


def test_non_json_serializable_field_falls_back_to_str(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque-repr"

    log_info(logger, "has_opaque_field", thing=Opaque())

    assert _payload(caplog)["thing"] == "opaque-repr"
