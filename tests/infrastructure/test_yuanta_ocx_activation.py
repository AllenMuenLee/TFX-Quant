"""Opt-in real-OCX activation smoke test — skipped unless TFX_QUANT_REAL_API=1.

Only verifies that the registered Yuanta order OCX can be instantiated on this machine.
It never logs in and never submits an order.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.real_api


def test_order_ocx_control_is_registered_and_instantiable() -> None:
    from tfx_quant.infrastructure.yuanta.legacy_ocx_host import (
        is_control_registered,
        yuanta_control_progid,
    )

    progid = yuanta_control_progid()
    assert is_control_registered(progid), (
        f"Yuanta order OCX {progid!r} is not registered — run regsvr32 on the vendor OCX"
    )
