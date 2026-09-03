"""持倉核對 — the domain model behind comparing the broker's actual position against
this system's own locally-derived "expected position", plus the manual-sync gate and
audit-record shapes built on top of it.

Real position truth only ever comes from `application.ports.yuanta_gateways.
TradeGatewayPort.query_positions()` — never inferred, never assumed. This module's
`PositionBaseline` is this system's own *belief* about what that query should return,
built up incrementally from fills it has itself observed (see
`application.position_reconciliation.reconciliation_service.
PositionReconciliationService`) and only ever replaced outright by an explicit,
human-confirmed manual sync — never silently adjusted to match a broker query on its
own, per the "不得把持倉差異自動解釋成策略成交" rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.quantity import NetPosition
from tfx_quant.domain.timestamp import Timestamp


class ReconciliationTrigger(StrEnum):
    """Every required trigger point from the implementation prompt: "登入、啟動策略、
    每次成交、反手全平閘門、重連、定時輪詢及回到前景時查詢持倉與活動委託."""

    LOGIN = "LOGIN"
    STRATEGY_START = "STRATEGY_START"
    FILL = "FILL"
    REVERSAL_FLAT_GATE = "REVERSAL_FLAT_GATE"
    RECONNECT = "RECONNECT"
    TIMED_POLL = "TIMED_POLL"
    FOREGROUND_RETURN = "FOREGROUND_RETURN"
    MANUAL_REQUERY = "MANUAL_REQUERY"


class DiscrepancyKind(StrEnum):
    NONE = "NONE"
    DIRECTION = "DIRECTION"
    """Expected and actual disagree on sign (long vs. short, or flat vs. non-flat)."""
    QUANTITY = "QUANTITY"
    """Same sign (or both flat, which can't reach this branch), different magnitude."""
    OTHER_CONTRACT = "OTHER_CONTRACT"
    """The broker reports a non-flat position outside the locally selected contract."""


def classify_discrepancy(expected: NetPosition, actual: NetPosition) -> DiscrepancyKind:
    """ "實際持倉與 expected position 在方向或口數上不同" as a pure, directly-testable
    classification rather than a single collapsed boolean."""
    if expected.lots == actual.lots:
        return DiscrepancyKind.NONE
    if _sign(expected.lots) != _sign(actual.lots):
        return DiscrepancyKind.DIRECTION
    return DiscrepancyKind.QUANTITY


def _sign(lots: int) -> int:
    return (lots > 0) - (lots < 0)


SUSPECTED_CAUSE_HYPOTHESES: tuple[str, ...] = (
    "mobile_app_or_web_terminal",
    "manual_broker_terminal_action",
    "other_automated_program",
)
"""Logged verbatim on every discrepancy — this system has no way to actually
distinguish which of these produced an out-of-band position change (手機 App／人工／
其他程式), so it honestly lists every candidate hypothesis every time rather than
pretending to detect one specifically. Never used to change behavior, only ever logged."""


@dataclass(frozen=True, slots=True)
class PositionBaseline:
    """The persisted "expected position" this system currently believes is true for one
    (account, instrument, contract) — the real implementation of the `OrderManager`
    `position_lookup` seam that Feature 06 left as an always-flat placeholder (see
    `desktop.composition._flat_position_lookup`'s docstring).

    Starts at an assumed-flat 0 lots the first time a contract is ever seen
    (`source="assumed_flat_at_first_use"`); every fill this system's own `OrderManager`
    observes moves it incrementally (`source="fill"`); a confirmed manual sync replaces
    it outright with the broker's own authoritative query result
    (`source="manual_sync"`)."""

    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    expected_net: NetPosition
    updated_at: Timestamp
    source: str


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """One position-query-and-compare run's full structured result — "本地預期、券商
    實際、活動／未知委託、差異分類、採取動作及 readiness 結果" as independently-
    inspectable fields, never collapsed into free text. Not persisted (same as
    `domain.reversal_workflow.FlatConfirmationResult`) — logged via
    `application.position_reconciliation.reconciliation_service` and returned to
    callers (e.g. the manual-sync UI flow's "重新查詢" step) for direct display."""

    correlation_id: str
    query_id: str
    trigger: ReconciliationTrigger
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    expected_net: NetPosition
    actual_net: NetPosition | None
    """`None` only when the broker query itself failed — see `query_error`."""
    broker_snapshot_at: Timestamp | None
    """The broker's own "as of" timestamp for `actual_net` (`Position.as_of`) — the
    "時間" the manual-sync confirmation button must display, distinct from `at` (when
    this record itself was built). `None` when the query failed or returned no row
    (implicitly flat, no broker-reported snapshot instant)."""
    discrepancy: DiscrepancyKind
    has_active_or_unknown_orders: bool
    active_or_unknown_order_count: int
    other_contract_position_count: int
    paused: bool
    """Whether this run drove the strategy toward `StrategyState.PAUSED_SAFE`/
    `FAULTED`. `False` for a matched query, a skipped query (no selection yet), or a
    failed query — a transient query failure is never itself treated as a discrepancy."""
    resulting_strategy_state: str | None
    possible_causes: tuple[str, ...]
    query_duration_seconds: float
    query_error: str | None
    at: Timestamp

    @property
    def query_succeeded(self) -> bool:
        return self.query_error is None


@dataclass(frozen=True, slots=True)
class ManualSyncPreflight:
    """The "同步前必須確認無活動或未知委託" gate — same itemized-boolean shape as
    `domain.reversal_workflow.FlatConfirmationResult`, never a single collapsed check."""

    has_active_orders: bool
    has_unknown_orders: bool

    @property
    def allowed(self) -> bool:
        return not (self.has_active_orders or self.has_unknown_orders)


@dataclass(frozen=True, slots=True)
class ManualSyncRecord:
    """The audit record for one confirmed manual sync — "操作者動作、確認內容遮罩、
    同步前後 baseline、訊號重置及是否仍保持 PausedSafe" tied together by
    `correlation_id`. Not persisted — logged, and returned to the caller for display."""

    correlation_id: str
    account: TradingAccount
    instrument: Instrument
    contract: ContractMonth
    baseline_before: NetPosition
    baseline_after: NetPosition
    broker_snapshot_at: Timestamp
    bar_signal_state_reset: bool
    reversal_workflow_reset: bool
    resulting_strategy_state: str | None
    still_paused_safe: bool
    at: Timestamp


__all__ = [
    "SUSPECTED_CAUSE_HYPOTHESES",
    "DiscrepancyKind",
    "ManualSyncPreflight",
    "ManualSyncRecord",
    "PositionBaseline",
    "ReconciliationRecord",
    "ReconciliationTrigger",
    "classify_discrepancy",
]
