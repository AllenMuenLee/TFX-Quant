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
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.quantity import MAX_LOTS

REQUIRED_TIMEZONE_ID = "Asia/Taipei"
REQUIRED_EOD_FLATTEN_TIME = time(4, 55)


class Environment(StrEnum):
    TEST = "TEST"
    PRODUCTION = "PRODUCTION"


class ContractSelectionMode(StrEnum):
    AUTO = "AUTO"


class SettingsValidationError(ValueError):
    """Raised by `validate_startup()` — the one exception type startup config failures
    surface as, independent of the underlying validation library."""


class SimulationFeeModelSettings(BaseModel):
    """The configurable, versioned cost model the local broker simulator applies to a
    simulated fill (測試環境). Never consulted for a real Yuanta
    fill — those carry `provisional` costs until the broker's own fee data is confirmed.
    A missing field (`None`) leaves that cost unknown, so the simulated fill is itself
    flagged `provisional` rather than silently priced at zero."""

    model_config = {"frozen": True, "extra": "forbid"}

    version: str
    """Stamped onto every simulated fill's audit log so a P&L number can be tied to the
    exact cost model that produced it (Feature 15: "成交模型…須…具版本並完整記錄")."""
    commission_per_lot: Decimal | None = None
    """TWD commission charged per filled lot, each side."""
    tax_rate: Decimal | None = None
    """TAIFEX 期交稅 rate applied to contract notional (price × multiplier × lots) on the
    closing side, e.g. `0.00002` (十萬分之二) for index futures."""
    slippage_ticks: Decimal | None = None
    """Adverse price movement, in ticks, applied against the order side at fill time.
    `None` or `0` means fills are simulated at the reference price with no slippage."""

    @field_validator("version")
    @classmethod
    def _version_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("simulation_fee_model.version must not be blank")
        return v

    @field_validator("commission_per_lot", "tax_rate", "slippage_ticks")
    @classmethod
    def _non_negative(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise ValueError("simulation_fee_model cost fields must not be negative")
        return v


class TradingSettings(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    account_alias: str
    """A non-secret label (e.g. "primary"), never the raw broker account number."""
    quote_account_alias: str | None = None
    """A non-secret label for the quote-API login used by UAT 交易模擬模式 (keyed separately
    from the trade credential — see `infrastructure.yuanta.credentials.
    QUOTE_KEYRING_SERVICE_NAME`). `None` in 正式環境."""
    environment: Environment
    selected_instrument: Instrument
    contract_selection_mode: ContractSelectionMode = ContractSelectionMode.AUTO
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
    eod_flatten_workflow_db_path: str | None = None
    """Path to the SQLite database file `application.risk.risk_supervisor.
    RiskSupervisor` persists 04:55/emergency flatten workflows to (see
    `application.ports.eod_flatten_workflow_repository` and `persistence.
    sqlite_eod_flatten_workflow_repository`). `None` falls back to a per-user data
    directory (`%LOCALAPPDATA%/tfx_quant/eod_flatten_workflows.sqlite3` on Windows) —
    see `desktop.composition._resolve_eod_flatten_workflow_db_path`. Deliberately a
    separate file from every other `*_db_path`, never the same connection — same
    lock-hazard reasoning as every other dedicated workflow database in this codebase."""
    fill_ledger_db_path: str | None = None
    """Path to the SQLite database file `application.trade_reports.fill_ledger_service.
    FillLedgerService` persists the append-only execution ledger to (see `application.
    ports.fill_ledger_repository` and `persistence.sqlite_fill_ledger_repository`).
    `None` falls back to a per-user data directory
    (`%LOCALAPPDATA%/tfx_quant/fill_ledger.sqlite3` on Windows) — see
    `desktop.composition._resolve_fill_ledger_db_path`. Deliberately a separate file
    from every other `*_db_path`, never the same connection — same lock-hazard reasoning
    as every other dedicated database in this codebase."""
    audit_db_path: str | None = None
    """Path to the SQLite audit-event database (`telemetry.audit.SqliteAuditHandler`), read
    back by the trade-report drill-down to reconstruct a workflow's decision→fill→P&L
    timeline. `None` falls back to `%LOCALAPPDATA%/tfx_quant/logs/audit.sqlite3` — the path
    `desktop.__main__` installs the handler at."""
    simulation_fee_model: SimulationFeeModelSettings | None = None
    """The versioned cost model the local broker simulator applies to simulated fills
    (測試環境). `None` in 正式環境 — real fills never consult
    it. Required for a 測試環境 settings file that wants realistic simulated P&L; absent, the
    simulator marks every simulated fill's costs `provisional`."""

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


def validate_startup(raw: dict[str, Any]) -> TradingSettings:
    """Construct and validate settings from a raw dict (e.g. parsed JSON).

    Raises `SettingsValidationError` with a clear, combined message on any failure —
    the single place a startup config problem is reported from.
    """
    try:
        return TradingSettings.model_validate(raw)
    except ValidationError as exc:
        raise SettingsValidationError(str(exc)) from exc
