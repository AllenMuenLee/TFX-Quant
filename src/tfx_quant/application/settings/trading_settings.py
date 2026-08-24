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
    instrument_master_path: str | None = None
    """Path to the controlled 商品主檔 JSON file (see `application.ports.
    instrument_master`). `None` falls back to the bundled example/seed file — fine for
    the bundled sample, but live use requires a real, Yuanta-confirmed file (see
    `infrastructure/yuanta/instrument_master.example.json`'s own warning)."""
    trading_calendar_path: str | None = None
    """Path to the controlled 交易日曆 JSON file (see `application.ports.
    trading_calendar`). `None` falls back to the bundled example/seed file — its
    holiday dates are a best-effort web-search seed, not yet confirmed against TAIFEX's
    official calendar (see `infrastructure/market_data/trading_calendar.example.json`'s
    own warning)."""
    yahoo_ticker_mapping_path: str | None = None
    """Path to the controlled 內部商品／契約 -> Yahoo ticker JSON file (see
    `application.ports.yahoo_ticker_mapping`). `None` falls back to the bundled
    example/seed file, whose `mappings` array is deliberately empty — no confirmed
    Yahoo Finance ticker for any TAIFEX futures contract has been verified (see
    `infrastructure/market_data/yahoo_ticker_mapping.example.json`'s own warning), so
    the yfinance backfill simply finds nothing to query until an operator supplies a
    real, confirmed mapping."""
    market_data_db_path: str | None = None
    """Path to the SQLite database file this software persists its own self-aggregated
    two-month 60-minute bar history to (see `application.ports.bar_record_repository`
    and `persistence.sqlite_bar_record_repository`). `None` falls back to a per-user
    data directory (`%LOCALAPPDATA%/tfx_quant/market_data.sqlite3` on Windows) —
    see `desktop.composition._resolve_market_data_db_path`."""
    order_db_path: str | None = None
    """Path to the SQLite database file `OrderManager` persists order intents to (see
    `application.ports.order_repository` and `persistence.sqlite_order_repository`).
    `None` falls back to a per-user data directory
    (`%LOCALAPPDATA%/tfx_quant/orders.sqlite3` on Windows) — see
    `desktop.composition._resolve_order_db_path`. Deliberately a separate file from
    `market_data_db_path`, never the same connection — see
    `docs/adr/0008-order-and-fill-state-machine.md`."""
    reversal_workflow_db_path: str | None = None
    """Path to the SQLite database file `ReversalWorkflowService` persists reversal
    workflows to (see `application.ports.reversal_workflow_repository` and
    `persistence.sqlite_reversal_workflow_repository`). `None` falls back to a per-user
    data directory (`%LOCALAPPDATA%/tfx_quant/reversal_workflows.sqlite3` on Windows) —
    see `desktop.composition._resolve_reversal_workflow_db_path`. Deliberately a
    separate file from `order_db_path`/`market_data_db_path`, never the same connection
    — see `docs/adr/0009-safe-reversal-and-scaling.md`."""
    position_baseline_db_path: str | None = None
    """Path to the SQLite database file `PositionReconciliationService` persists its
    expected-position baselines to (see `application.ports.position_baseline_repository`
    and `persistence.sqlite_position_baseline_repository`). `None` falls back to a
    per-user data directory (`%LOCALAPPDATA%/tfx_quant/position_baselines.sqlite3` on
    Windows) — see `desktop.composition._resolve_position_baseline_db_path`.
    Deliberately a separate file from every other `*_db_path`, never the same connection
    — see `docs/adr/0010-position-reconciliation-and-manual-sync.md`."""

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
