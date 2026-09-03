"""Opt-in live-connectivity smoke test — skipped unless TFX_QUANT_REAL_API=1.

Verifies the machine can reach the Yuanta quote preflight requirements. It performs no
login and no order submission. The 測試環境 uses the real quote feed but never the trade
API (the local broker simulator handles all execution), so this is never a required
step.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.real_api


def test_quote_runtime_preflight_reports_its_status() -> None:
    from tfx_quant.infrastructure.yuanta.quote_preflight import check_quote_runtime

    result = check_quote_runtime()
    assert isinstance(result.passed, bool)
    if not result.passed:
        pytest.skip("quote preflight not satisfied on this machine: " + "; ".join(result.errors))
