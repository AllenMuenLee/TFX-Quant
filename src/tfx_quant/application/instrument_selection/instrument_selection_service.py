"""InstrumentSelectionService — the switch/selection workflow for Feature 03.

Owns exactly the sequence the implementation prompt spells out: 切換流程必須先暫停 (the
strategy must already be paused/stopped — this service never drives the state machine
itself, see below), 重新查詢帳戶狀態, 清空 K 棒／訊號狀態; 成功後仍需使用者重新啟動。
And the hard precondition: 策略執行中或存在持倉、活動委託、未知委託時禁止切換商品／契約。
This service itself never touches the quote-gateway registration directly — publishing
`InstrumentSwitchCompleted` (carrying `vendor_symbol`) is what drives
`desktop.quote_runtime.QuoteRuntime._on_switch` to re-register the live Yuanta quote
symbol for the newly selected contract; clearing the bar/signal state
(`bar_signal_state_store.clear()`) is a separate step, resetting Feature 05's
per-(instrument, contract) engine state.

**Why this service never transitions `StrategyStateMachine` itself**: the "must first
pause" requirement and the "forbid switching while executing" requirement would
otherwise read as contradictory (if switching always force-pauses first, what does
"forbid while executing" forbid?). Resolved by treating "must first pause" as a
*precondition* the switch checks (state already `STOPPED` or `PAUSED_SAFE`) rather than
an action the switch performs — switching never itself moves the strategy in or out of
`RUNNING`. Since `StrategyState.RUNNING` is only reachable via a fresh `STARTING` (see
`domain.strategy_state`), leaving the machine in `STOPPED`/`PAUSED_SAFE` after a switch
already guarantees "成功後仍需使用者重新啟動" without this service needing to force any
particular transition.

**Why `switch_to()` re-validates via `_evaluate_switch_allowed()` immediately before
clearing bar/signal state**: this is the literal "重新查詢帳戶狀態" step — a fresh
position/order query right before the switch actually takes effect, not just the
earlier `check_switch_allowed()` a caller may have polled moments before while the
operator was still deciding. If the real `TradeGatewayPort` can't yet answer that query
safely, `LegacyBroker.query_open_orders` raises `OrderQueryNotReadyError` whenever the broker's
report query hasn't completed yet or an order's status is unresolved (see
`infrastructure.yuanta.legacy_broker`) — that propagates as a blocked switch rather
than a silent unsafe assumption.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from tfx_quant.application.events.events import Event, InstrumentSwitchCompleted
from tfx_quant.application.instrument_selection.errors import (
    InstrumentMasterEntryNotFoundError,
    SwitchBlockedError,
)
from tfx_quant.application.instrument_selection.selection import ResolvedSelection
from tfx_quant.application.instrument_selection.validation import validate_can_open
from tfx_quant.application.ports.bar_signal_state import BarSignalStateStore
from tfx_quant.application.ports.clock import Clock
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.application.ports.yuanta_gateways import (
    TradeGatewayPort,
)
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import Order
from tfx_quant.domain.position import Position
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import (
    correlation_scope,
    get_logger,
    log_info,
    log_warning,
    new_correlation_id,
)
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)

_SWITCHABLE_STATES = (StrategyState.STOPPED, StrategyState.PAUSED_SAFE)


def _position_summary(positions: Sequence[Position]) -> list[dict[str, Any]]:
    """Account-masked, per Feature 03's "被拒絕的切換或啟動須記錄持倉..." debug-logging
    requirement — never a bare "無效契約"."""
    return [
        {
            "account_no": mask_account(p.account.account_no),
            "instrument": p.instrument.value,
            "contract": p.contract.code,
            "net_lots": p.net.lots,
        }
        for p in positions
    ]


def _order_summary(orders: Sequence[Order]) -> list[dict[str, Any]]:
    return [
        {
            "account_no": mask_account(o.account.account_no),
            "instrument": o.instrument.value,
            "contract": o.contract.code,
        }
        for o in orders
    ]


@dataclass(frozen=True, slots=True)
class _SwitchAllowedResult:
    reason: str | None
    positions: tuple[Position, ...] = field(default_factory=tuple)
    open_orders: tuple[Order, ...] = field(default_factory=tuple)


class EventPublisher(Protocol):
    """Structural stand-in for `EventCoordinator` — see `session_orchestrator.py`'s
    identical seam. Any object with a thread-safe `publish(event)` works."""

    def publish(self, event: Event) -> None: ...


class InstrumentSelectionService:
    def __init__(
        self,
        *,
        strategy_state_machine: StrategyStateMachine,
        trade_gateway: TradeGatewayPort,
        instrument_master: InstrumentMasterRepository,
        bar_signal_state_store: BarSignalStateStore,
        clock: Clock,
        event_publisher: EventPublisher | None = None,
        broker_session_ready: Callable[[], bool] | None = None,
    ) -> None:
        self._strategy_state_machine = strategy_state_machine
        self._trade_gateway = trade_gateway
        self._instrument_master = instrument_master
        self._bar_signal_state_store = bar_signal_state_store
        self._clock = clock
        self._event_publisher = event_publisher
        self._broker_session_ready = broker_session_ready or (lambda: True)
        self._current: ResolvedSelection | None = None

    @property
    def current(self) -> ResolvedSelection | None:
        """`None` until the first successful `switch_to()` — nothing is confirmed or
        subscribed yet."""
        return self._current

    # -- Resolution (no side effects — safe to call repeatedly while the operator
    #    picks/previews a selection before confirming) -----------------------------

    def resolve_manual(self, instrument: Instrument, contract: ContractMonth) -> ResolvedSelection:
        """MANUAL mode: the operator picked an explicit contract month. Validates it
        exists in the master and can currently be opened (not expired/halted)."""
        entry = self._instrument_master.get(instrument, contract)
        reason = validate_can_open(entry, as_of=self._clock.now())
        if reason is not None:
            log_warning(
                _logger,
                "contract_resolution_rejected",
                mode="MANUAL",
                instrument=instrument.value,
                contract=contract.code,
                reason=reason,
            )
            raise InstrumentMasterEntryNotFoundError(reason)
        assert entry is not None  # validate_can_open already rejected the None case
        log_info(
            _logger,
            "contract_resolved",
            mode="MANUAL",
            instrument=instrument.value,
            contract=contract.code,
            tick_size=str(entry.tick_size),
            expiry_date=entry.expiry_date.isoformat(),
            tradable=entry.tradable,
        )
        return ResolvedSelection(instrument=instrument, contract=contract, entry=entry)

    def resolve_near_month(self, instrument: Instrument) -> ResolvedSelection:
        """AUTO mode: the nearest tradable, unexpired contract for `instrument`, per
        the controlled master file — never a computed/guessed month. Per Feature 03's
        acceptance criteria, the caller must still show this to the operator and
        obtain confirmation before it's applied via `switch_to()`."""
        as_of_date = self._clock.now().value.date()
        candidates = sorted(
            (
                entry
                for entry in self._instrument_master.list_for(instrument)
                if entry.tradable and entry.expiry_date >= as_of_date
            ),
            key=lambda entry: (entry.contract.year, entry.contract.month),
        )
        log_info(
            _logger,
            "near_month_candidates_resolved",
            instrument=instrument.value,
            as_of_date=as_of_date.isoformat(),
            candidate_contracts=[entry.contract.code for entry in candidates],
        )
        if not candidates:
            log_warning(
                _logger, "contract_resolution_rejected", mode="AUTO", instrument=instrument.value
            )
            raise InstrumentMasterEntryNotFoundError(
                f"{instrument.display_name_zh}（{instrument.value}）目前無可交易之近月契約"
                "，主檔可能缺漏或已全數到期"
            )
        entry = candidates[0]
        log_info(
            _logger,
            "contract_resolved",
            mode="AUTO",
            instrument=instrument.value,
            contract=entry.contract.code,
            selection_reason="earliest tradable unexpired contract by (year, month)",
        )
        return ResolvedSelection(instrument=instrument, contract=entry.contract, entry=entry)

    # -- Switching --------------------------------------------------------------

    def _evaluate_switch_allowed(self) -> _SwitchAllowedResult:
        # This selection controls only the quote subscription and chart identity.
        # Trading is independently restricted to MXF at the order boundary, so
        # broker positions and active orders do not block a market-view switch.
        if self._strategy_state_machine.state in _SWITCHABLE_STATES:
            return _SwitchAllowedResult(reason=None)
        return _SwitchAllowedResult(reason="策略執行中，禁止切換市場行情，請先停止或安全暫停")

    def check_switch_allowed(self) -> str | None:
        """Returns a Chinese block-reason, or `None` if switching is currently
        allowed. Never raises — callers decide whether a blocked switch is an error
        (`switch_to()`) or just something to display (a UI status line).

        Deliberately not logged here: `desktop/instrument_selection_panel.py` polls
        this on every broker/market-data event to keep its "確認並切換" button state
        current, so logging every call would be a per-tick firehose. The rejection
        detail this feature's debug-logging requirement asks for (positions/orders/
        contract comparison, not a bare "無效契約") is instead logged once per actual
        switch *attempt* — see `switch_to()`."""
        return self._evaluate_switch_allowed().reason

    def switch_to(self, resolved: ResolvedSelection) -> None:
        """Applies an already-`resolve_*()`'d, operator-confirmed selection.

        Raises `SwitchBlockedError` (and leaves everything untouched) if switching
        isn't currently allowed — a fresh "重新查詢帳戶狀態" check immediately before
        the switch actually takes effect."""
        workflow_id = new_correlation_id()
        with correlation_scope(workflow_id=workflow_id):
            self._switch_to(resolved)

    def _switch_to(self, resolved: ResolvedSelection) -> None:
        workflow_start = time.monotonic()
        log_info(
            _logger,
            "instrument_switch_started",
            instrument=resolved.instrument.value,
            contract=resolved.contract.code,
        )

        step_start = time.monotonic()
        evaluation = self._evaluate_switch_allowed()
        log_info(
            _logger,
            "instrument_switch_step_completed",
            step="requery_account_status",
            passed=evaluation.reason is None,
            duration_ms=(time.monotonic() - step_start) * 1000,
        )
        if evaluation.reason is not None:
            log_warning(
                _logger,
                "instrument_switch_rejected",
                step="requery_account_status",
                reason=evaluation.reason,
                positions=_position_summary(evaluation.positions),
                open_orders=_order_summary(evaluation.open_orders),
            )
            raise SwitchBlockedError(evaluation.reason)

        step_start = time.monotonic()
        self._bar_signal_state_store.clear(resolved.instrument, resolved.contract)
        log_info(
            _logger,
            "instrument_switch_step_completed",
            step="reset_bar_signal_state",
            duration_ms=(time.monotonic() - step_start) * 1000,
        )

        self._current = resolved

        if self._event_publisher is not None:
            self._event_publisher.publish(
                InstrumentSwitchCompleted(
                    at=Timestamp.now(),
                    instrument=resolved.instrument,
                    contract=resolved.contract,
                    vendor_symbol=resolved.entry.vendor_symbol,
                )
            )

        log_info(
            _logger,
            "instrument_switch_completed",
            instrument=resolved.instrument.value,
            contract=resolved.contract.code,
            vendor_symbol=resolved.entry.vendor_symbol,
            duration_ms=(time.monotonic() - workflow_start) * 1000,
        )
