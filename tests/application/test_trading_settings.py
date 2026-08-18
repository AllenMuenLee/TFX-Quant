from __future__ import annotations

from typing import Any

import pytest

from tfx_quant.application.settings.trading_settings import (
    ContractSelectionMode,
    SettingsValidationError,
    validate_startup,
)


def test_valid_settings_load_successfully(valid_settings_raw: dict[str, Any]) -> None:
    settings = validate_startup(valid_settings_raw)
    assert settings.account_alias == "primary"
    assert settings.max_net_lots == 2


def test_wrong_timezone_is_rejected(valid_settings_raw: dict[str, Any]) -> None:
    valid_settings_raw["timezone_id"] = "UTC"
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


def test_wrong_flatten_time_is_rejected(valid_settings_raw: dict[str, Any]) -> None:
    valid_settings_raw["eod_flatten_local_time"] = "09:00:00"
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


@pytest.mark.parametrize("max_net_lots", [0, 3, -1])
def test_lot_cap_above_two_or_non_positive_is_rejected(
    valid_settings_raw: dict[str, Any], max_net_lots: int
) -> None:
    valid_settings_raw["max_net_lots"] = max_net_lots
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


def test_undefined_instrument_is_rejected(valid_settings_raw: dict[str, Any]) -> None:
    valid_settings_raw["selected_instrument"] = "SPX"
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


def test_manual_contract_mode_without_contract_is_rejected(
    valid_settings_raw: dict[str, Any],
) -> None:
    valid_settings_raw["contract_selection_mode"] = "MANUAL"
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


def test_manual_contract_mode_with_invalid_month_is_rejected(
    valid_settings_raw: dict[str, Any],
) -> None:
    valid_settings_raw["contract_selection_mode"] = "MANUAL"
    valid_settings_raw["manual_contract_year"] = 2026
    valid_settings_raw["manual_contract_month"] = 13
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


def test_manual_contract_mode_with_valid_contract_succeeds(
    valid_settings_raw: dict[str, Any],
) -> None:
    valid_settings_raw["contract_selection_mode"] = "MANUAL"
    valid_settings_raw["manual_contract_year"] = 2026
    valid_settings_raw["manual_contract_month"] = 9
    settings = validate_startup(valid_settings_raw)
    assert settings.contract_selection_mode is ContractSelectionMode.MANUAL
    assert settings.manual_contract() is not None
    assert settings.manual_contract().code == "202609"  # type: ignore[union-attr]


def test_blank_account_alias_is_rejected(valid_settings_raw: dict[str, Any]) -> None:
    valid_settings_raw["account_alias"] = "   "
    with pytest.raises(SettingsValidationError):
        validate_startup(valid_settings_raw)


def test_settings_are_frozen(valid_settings_raw: dict[str, Any]) -> None:
    settings = validate_startup(valid_settings_raw)
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises its own frozen error
        settings.account_alias = "changed"  # type: ignore[misc]
