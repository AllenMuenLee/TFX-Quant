"""InstrumentSelectionService — the switch/selection workflow for Feature 03.

Owns exactly the sequence the implementation prompt spells out: 切換流程必須先暫停 (the
strategy must already be paused/stopped — this service never drives the state machine
itself, see below), 取消舊行情訂閱, 重新查詢帳戶狀態, 訂閱新契約並清空 K 棒／訊號狀態;
成功後仍需使用者重新啟動。And the hard precondition: 策略執行中或存在持倉、活動委託、
未知委託時禁止切換商品／契約。

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

**Why `switch_to()` calls `check_switch_allowed()` twice**: once before touching
anything, and again after cancelling the old quote subscription but before subscribing
the new one — this second call is the literal "重新查詢帳戶狀態" step, guarding against
a position/order appearing in the window between the first check and the switch
actually happening. If the real `TradeGatewayPort` can't yet answer that query (Feature
02's `BrokerSessionTradeGatewayView.query_open_orders/positions` still raise
`NotImplementedError` — structured parsing is a documented Feature 02/08 gap), that
propagates as a blocked switch rather than a silent unsafe assumption.
"""

from __future__ import annotations

from typing import Protocol

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
from tfx_quant.application.ports.yuanta_gateways import QuoteGatewayPort, TradeGatewayPort
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.strategy_state import StrategyState, StrategyStateMachine
from tfx_quant.domain.timestamp import Timestamp

_SWITCHABLE_STATES = (StrategyState.STOPPED, StrategyState.PAUSED_SAFE)


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
        quote_gateway: QuoteGatewayPort,
        instrument_master: InstrumentMasterRepository,
        bar_signal_state_store: BarSignalStateStore,
        clock: Clock,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self._strategy_state_machine = strategy_state_machine
        self._trade_gateway = trade_gateway
        self._quote_gateway = quote_gateway
        self._instrument_master = instrument_master
        self._bar_signal_state_store = bar_signal_state_store
        self._clock = clock
        self._event_publisher = event_publisher
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
            raise InstrumentMasterEntryNotFoundError(reason)
        assert entry is not None  # validate_can_open already rejected the None case
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
        if not candidates:
            raise InstrumentMasterEntryNotFoundError(
                f"{instrument.display_name_zh}（{instrument.value}）目前無可交易之近月契約"
                "，主檔可能缺漏或已全數到期"
            )
        entry = candidates[0]
        return ResolvedSelection(instrument=instrument, contract=entry.contract, entry=entry)

    # -- Switching --------------------------------------------------------------

    def check_switch_allowed(self) -> str | None:
        """Returns a Chinese block-reason, or `None` if switching is currently
        allowed. Never raises — callers decide whether a blocked switch is an error
        (`switch_to()`) or just something to display (a UI status line)."""
        if self._strategy_state_machine.state not in _SWITCHABLE_STATES:
            return "策略執行中，禁止切換商品／契約，請先停止或暫停策略"
        try:
            positions = self._trade_gateway.query_positions()
            open_orders = self._trade_gateway.query_open_orders()
        except NotImplementedError:
            return (
                "尚無法查詢帳戶持倉／委託狀態（結構化查詢尚未實作），為安全起見禁止切換商品／契約"
            )
        if any(not position.net.is_flat for position in positions):
            return "尚有持倉，禁止切換商品／契約"
        if open_orders:
            return "尚有活動委託或狀態不明委託，禁止切換商品／契約"
        return None

    def switch_to(self, resolved: ResolvedSelection) -> None:
        """Applies an already-`resolve_*()`'d, operator-confirmed selection.

        Raises `SwitchBlockedError` (and leaves everything untouched) if switching
        isn't currently allowed — either before starting, or after cancelling the old
        subscription (the "重新查詢帳戶狀態" re-check)."""
        reason = self.check_switch_allowed()
        if reason is not None:
            raise SwitchBlockedError(reason)

        previous = self._current
        if previous is not None:
            self._quote_gateway.unsubscribe(previous.instrument, previous.contract)

        reason = self.check_switch_allowed()
        if reason is not None:
            if previous is not None:
                # Best-effort: restore the old subscription rather than leaving the
                # account with no market data at all after an aborted switch.
                self._quote_gateway.subscribe(previous.instrument, previous.contract)
            raise SwitchBlockedError(f"重新查詢帳戶狀態後中止切換：{reason}")

        self._quote_gateway.subscribe(resolved.instrument, resolved.contract)
        self._bar_signal_state_store.clear(resolved.instrument, resolved.contract)
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
