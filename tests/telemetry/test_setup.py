from __future__ import annotations

import logging
from pathlib import Path

from tfx_quant.telemetry import get_logger, log_info
from tfx_quant.telemetry.setup import configure_logging, get_log_lines


def test_memory_log_is_human_readable_not_json(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    log_info(get_logger("test.viewer"), "quote_connected", instrument="MXF", lots=1)

    lines = get_log_lines()

    assert len(lines) == 1
    assert "INFO" in lines[0]
    assert "test.viewer" in lines[0]
    assert "quote_connected" in lines[0]
    assert "instrument=MXF" in lines[0]
    assert not lines[0].rstrip().endswith("}")


def test_memory_log_accepts_plain_logging_messages(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    logging.getLogger("test.plain").warning("plain message")

    assert "plain message" in get_log_lines()[0]
