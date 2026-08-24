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
        # Isolated per test — build_services() opens a real SQLite file at this path
        # (see desktop/composition.py's bar-history repository wiring); without this,
        # every test that calls build_services() would share one real on-disk database.
        "market_data_db_path": str(tmp_path / "market_data.sqlite3"),
        # Same isolation, for OrderManager's SqliteOrderRepository (Feature 06) — a
        # separate file/connection from market_data_db_path, never shared (see
        # docs/adr/0008-order-and-fill-state-machine.md).
        "order_db_path": str(tmp_path / "orders.sqlite3"),
        # Same isolation again, for ReversalWorkflowService's
        # SqliteReversalWorkflowRepository (Feature 07).
        "reversal_workflow_db_path": str(tmp_path / "reversal_workflows.sqlite3"),
        # Same isolation again, for PositionReconciliationService's
        # SqlitePositionBaselineRepository (Feature 08).
        "position_baseline_db_path": str(tmp_path / "position_baselines.sqlite3"),
    }
