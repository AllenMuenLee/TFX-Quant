"""TradingSettings — strongly-typed, validated startup configuration.

Never holds credentials (see docs/secrets-management.md — broker login secrets are
read separately, straight from an OS-level source, and never pass through this model
or its JSON file). `validate_startup()` is the single place a malformed config fails
loudly with a clear message, instead of letting the strategy engine start with bad
configuration (wrong time zone, wrong flatten time, lot cap above 2, undefined
instrument, incomplete manual contract selection).
"""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import DomainError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.quantity import MAX_LOTS

REQUIRED_TIMEZONE_ID = "Asia/Taipei"
REQUIRED_EOD_FLATTEN_TIME = time(4, 55)


class Environment(StrEnum):
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class ContractSelectionMode(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class SettingsValidationError(ValueError):
    """Raised by `validate_startup()` — the one exception type startup config failures
    surface as, independent of the underlying validation library."""


class TradingSettings(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    account_alias: str
    """A non-secret label (e.g. "primary"), never the raw broker account number."""
    environment: Environment
    selected_instrument: Instrument
    contract_selection_mode: ContractSelectionMode
    manual_contract_year: int | None = None
    manual_contract_month: int | None = None
    timezone_id: str = REQUIRED_TIMEZONE_ID
    eod_flatten_local_time: time = REQUIRED_EOD_FLATTEN_TIME
    max_net_lots: int = MAX_LOTS
    use_mock: bool = True
    """Selects mock vs. real Yuanta gateways in the desktop composition root."""
    instrument_master_path: str | None = None
    """Path to the controlled 商品主檔 JSON file (see `application.ports.
    instrument_master`). `None` falls back to the bundled example/seed file — fine for
    `use_mock: true`, but production use requires a real, Yuanta-confirmed file (see
    `infrastructure/yuanta/instrument_master.example.json`'s own warning)."""
    trading_calendar_path: str | None = None
    """Path to the controlled 交易日曆 JSON file (see `application.ports.
    trading_calendar`). `None` falls back to the bundled example/seed file — its
    holiday dates are a best-effort web-search seed, not yet confirmed against TAIFEX's
    official calendar (see `infrastructure/market_data/trading_calendar.example.json`'s
    own warning)."""

    @field_validator("account_alias")
    @classmethod
    def _account_alias_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("account_alias must not be blank")
        return v

    @field_validator("timezone_id")
    @classmethod
    def _timezone_must_be_taipei(cls, v: str) -> str:
        if v != REQUIRED_TIMEZONE_ID:
            raise ValueError(f'timezone_id must be "{REQUIRED_TIMEZONE_ID}", got {v!r}')
        return v

    @field_validator("eod_flatten_local_time")
    @classmethod
    def _flatten_time_must_be_0455(cls, v: time) -> time:
        if v != REQUIRED_EOD_FLATTEN_TIME:
            raise ValueError(f"eod_flatten_local_time must be 04:55, got {v}")
        return v

    @field_validator("max_net_lots")
    @classmethod
    def _max_lots_within_hard_cap(cls, v: int) -> int:
        if not (1 <= v <= MAX_LOTS):
            raise ValueError(f"max_net_lots must be between 1 and {MAX_LOTS}, got {v}")
        return v

    @model_validator(mode="after")
    def _manual_contract_required_when_manual(self) -> TradingSettings:
        if self.contract_selection_mode is ContractSelectionMode.MANUAL:
            if self.manual_contract_year is None or self.manual_contract_month is None:
                raise ValueError(
                    "manual_contract_year and manual_contract_month are both required "
                    "when contract_selection_mode is MANUAL"
                )
            try:
                ContractMonth(year=self.manual_contract_year, month=self.manual_contract_month)
            except DomainError as exc:
                raise ValueError(f"invalid manual contract: {exc}") from exc
        return self

    def manual_contract(self) -> ContractMonth | None:
        if self.manual_contract_year is None or self.manual_contract_month is None:
            return None
        return ContractMonth(year=self.manual_contract_year, month=self.manual_contract_month)


def validate_startup(raw: dict[str, Any]) -> TradingSettings:
    """Construct and validate settings from a raw dict (e.g. parsed JSON).

    Raises `SettingsValidationError` with a clear, combined message on any failure —
    the single place a startup config problem is reported from.
    """
    try:
        return TradingSettings.model_validate(raw)
    except ValidationError as exc:
        raise SettingsValidationError(str(exc)) from exc
