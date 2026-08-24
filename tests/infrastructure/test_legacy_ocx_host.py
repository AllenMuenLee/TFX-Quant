from __future__ import annotations

import struct
import threading
from typing import Any

import pytest

from tfx_quant.infrastructure.yuanta.legacy_ocx_host import (
    YuantaOcxHost,
    yuanta_control_progid,
)


class _FakeControl:
    def __init__(self) -> None:
        self.logout_count = 0

    def DoLogout(self) -> None:
        self.logout_count += 1


class _FakeFrame:
    def __init__(self) -> None:
        self.destroy_count = 0

    def Destroy(self) -> None:
        self.destroy_count += 1


def _unactivated_host() -> YuantaOcxHost:
    host = YuantaOcxHost.__new__(YuantaOcxHost)
    host._thread_id = threading.get_ident()
    host._handlers = {}
    host.control = _FakeControl()
    host._frame = _FakeFrame()
    host._advise_connection = object()
    return host


def test_progid_matches_python_bitness() -> None:
    suffix = ".64" if struct.calcsize("P") * 8 == 64 else ".1"
    assert yuanta_control_progid() == f"Yuanta.YuantaOrdCtrl{suffix}"


def test_host_routes_only_documented_events_and_rejects_duplicate_binding() -> None:
    host = _unactivated_host()
    received: list[tuple[Any, ...]] = []
    host.bind("OnLogonS", lambda *args: received.append(args))

    host._dispatch("OnLogonS", 2, "account")
    host._dispatch("OnOrdResult", 1, "ignored")

    assert received == [(2, "account")]
    with pytest.raises(ValueError, match="already bound"):
        host.bind("OnLogonS", lambda: None)
    with pytest.raises(ValueError, match="unsupported"):
        host.bind("OnUnknown", lambda: None)


def test_close_logs_out_destroys_container_and_is_idempotent() -> None:
    host = _unactivated_host()
    control = host.control
    frame = host._frame

    host.close()
    host.close()

    assert control.logout_count == 1
    assert frame.destroy_count == 1
    assert host._advise_connection is None
