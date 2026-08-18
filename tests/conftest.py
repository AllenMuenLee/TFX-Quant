from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def valid_settings_raw() -> dict[str, Any]:
    return {
        "account_alias": "primary",
        "environment": "TEST",
        "selected_instrument": "MXF",
        "contract_selection_mode": "AUTO",
        "timezone_id": "Asia/Taipei",
        "eod_flatten_local_time": "04:55:00",
        "max_net_lots": 2,
        "use_mock": True,
    }
