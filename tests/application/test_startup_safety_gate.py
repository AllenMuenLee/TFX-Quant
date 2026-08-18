from __future__ import annotations

import pytest

from tfx_quant.application.safety.startup_safety_gate import (
    SafetyChecklist,
    StartupSafetyGate,
)

CHECKLIST_FIELDS = (
    "api_logged_in",
    "account_confirmed",
    "market_data_valid",
    "order_query_completed",
    "position_synced",
    "no_unknown_orders",
    "instrument_contract_confirmed",
    "quote_position_order_consistent",
    "user_pressed_start",
)


def test_default_checklist_is_not_satisfied() -> None:
    assert not StartupSafetyGate.can_enter_running(SafetyChecklist())


def test_all_items_true_is_satisfied() -> None:
    checklist = SafetyChecklist(**dict.fromkeys(CHECKLIST_FIELDS, True))
    assert StartupSafetyGate.can_enter_running(checklist)


@pytest.mark.parametrize("missing_field", CHECKLIST_FIELDS)
def test_any_single_missing_item_blocks_running(missing_field: str) -> None:
    values = dict.fromkeys(CHECKLIST_FIELDS, True)
    values[missing_field] = False
    checklist = SafetyChecklist(**values)
    assert not StartupSafetyGate.can_enter_running(checklist)
    assert missing_field in checklist.unmet_items()


def test_unmet_items_lists_every_false_field() -> None:
    checklist = SafetyChecklist()
    assert set(checklist.unmet_items()) == set(CHECKLIST_FIELDS)
