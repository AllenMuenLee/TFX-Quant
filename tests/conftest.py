from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`real_api` tests hit a real Yuanta OCX; they are skipped unless opted in with
    `TFX_QUANT_REAL_API=1`, and even then must never submit an order (each carries its
    own no-send guard)."""
    if os.environ.get("TFX_QUANT_REAL_API") == "1":
        return
    skip = pytest.mark.skip(reason="real_api: set TFX_QUANT_REAL_API=1 to run (never sends orders)")
    for item in items:
        if "real_api" in item.keywords:
            item.add_marker(skip)


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
        # Same isolation again, for FillLedgerService's SqliteFillLedgerRepository
        # (Feature 11).
        "fill_ledger_db_path": str(tmp_path / "fill_ledger.sqlite3"),
    }
