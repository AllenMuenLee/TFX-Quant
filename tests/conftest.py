from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def valid_settings_raw(tmp_path: Path) -> dict[str, Any]:
    return {
        "account_alias": "primary",
        "environment": "TEST",
        "selected_instrument": "MXF",
        "contract_selection_mode": "AUTO",
        "timezone_id": "Asia/Taipei",
        "eod_flatten_local_time": "04:55:00",
        "max_net_lots": 2,
        "use_mock": True,
        # Isolated per test — build_services() opens a real SQLite file at this path
        # (see desktop/composition.py's bar-history repository wiring); without this,
        # every test that calls build_services() would share one real on-disk database.
        "market_data_db_path": str(tmp_path / "market_data.sqlite3"),
    }
