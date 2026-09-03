"""60分鐘策略訊號引擎 — a deterministic, broker/UI-independent state machine.

Consumes only already-confirmed inputs — closed `Bar`s (Feature 04's `BarClosed`),
actual fill confirmations (real broker fills, never order acks/intents), and wall-clock
ticks — and produces `StrategyDecision`s carrying, at most, one `SignalKind` trading
intent. Never calls a broker, never treats a submitted order as filled; the caller
(application layer) is responsible for turning a decision's `signal_kind` into an actual
order via `OrderManager`, keyed by `intent_key` for idempotent, replay-safe submission.

**Combined-position P&L / max-favorable-point basis** (加碼前後如何計算整體部位獲利):
the stop-loss basis and the profit/pullback basis are the *same* single value — the most
recently filled lot's actual price — mirroring the spec's explicit stop-loss rule ("已建
立第2口...改以第2口實際成交價為共同停損基準"). The basis rebases the instant a new lot
fills; the already-tracked max-favorable-point value is never reset or recomputed under
the new basis, only carried forward and compared against future evaluations under
whatever basis is currently active. This was an explicit product decision (implementation
prompt 05 flags this formula as a blocker otherwise) — not one to change without asking.

**20MA slope**: an exact Decimal one-bar delta between the current and immediately
preceding closed bar's 20MA (see `domain.moving_average.determine_slope`) — also an
explicit product decision, not an implicit float sign check.

**Entry/EOD time gate**: a single recurring daily band, `[eod_flatten_local_time,
entry_gate_local_time)` (04:55–10:45 by default) — new entries/adds are blocked inside
this band; the 04:55 forced-flatten check fires only inside it. Both boundaries are
plain wall-clock time-of-day comparisons (no calendar/session lookup needed) because the
band never crosses midnight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from enum import StrEnum

from tfx_quant.domain.bar import Bar, CandleColor
from tfx_quant.domain.bar_aggregator import CandleStreakCounter
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidStrategyEngineError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.moving_average import (
    MaSlope,
    determine_slope,
    is_choppy,
    moving_average_series,
    recent_range,
)
from tfx_quant.domain.quantity import MAX_LOTS
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind
from tfx_quant.domain.timestamp import Timestamp

STRATEGY_VERSION = "1"

_DEFAULT_MA_WINDOW = 20
_DEFAULT_FLAT_LOOKBACK = 5
_DEFAULT_FLAT_THRESHOLD_POINTS = Decimal("10")
_DEFAULT_STOP_LOSS_POINTS = Decimal("300")
_DEFAULT_PROFIT_ACTIVATION_POINTS = Decimal("300")
_DEFAULT_PULLBACK_RATIO = Decimal("0.30")
_DEFAULT_ENTRY_GATE_LOCAL_TIME = time(10, 45)
_DEFAULT_EOD_FLATTEN_LOCAL_TIME = time(4, 55)


class PositionSide(StrEnum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class LotFill:
    """One confirmed opening fill (實際成交)."""

    price: Decimal
    at: Timestamp


@dataclass(frozen=True, slots=True)
class EngineConfig:
    ma_window: int = _DEFAULT_MA_WINDOW
    flat_lookback: int = _DEFAULT_FLAT_LOOKBACK
    flat_threshold_points: Decimal = _DEFAULT_FLAT_THRESHOLD_POINTS
    stop_loss_points: Decimal = _DEFAULT_STOP_LOSS_POINTS
    profit_activation_points: Decimal = _DEFAULT_PROFIT_ACTIVATION_POINTS
    pullback_ratio: Decimal = _DEFAULT_PULLBACK_RATIO
    max_lots: int = MAX_LOTS
    entry_gate_local_time: time = _DEFAULT_ENTRY_GATE_LOCAL_TIME
    eod_flatten_local_time: time = _DEFAULT_EOD_FLATTEN_LOCAL_TIME


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """One evaluation's full outcome — logged/persisted in full every time (triggered
    or not), per the implementation prompt's debug-log and decision-record requirements.
    """

    decision_id: str
    intent_key: str | None
    """Non-`None` only when `signal_kind` is non-`None` — the idempotency key the
    caller must pass through to `OrderManager`/`ScalingService`."""
    strategy_version: str
    at: Timestamp
    trigger: str
    """"bar_closed" or "clock_tick"."""
    bar_start: Timestamp | None
    bar_end: Timestamp | None
    current_price: Decimal | None
    """The last closed bar's close price at the time of this evaluation — the only
    price this engine ever sees, even though a live Yuanta quote feed exists elsewhere
    in this codebase for display/staleness purposes (this engine never reads it).
    Callers submitting an order for a signal use this as the order price."""
    candle_color: CandleColor | None
    streak_color: CandleColor | None
    streak_length: int
    ma_value: Decimal | None
    ma_previous_value: Decimal | None
    ma_slope: MaSlope
    ma_sample_count: int
    ma_recent_range: Decimal | None
    ma_is_choppy: bool
    entry_gate_open: bool
    data_reliable: bool
    has_active_order: bool
    position_state_uncertain: bool
    position_side: PositionSide
    position_lots: int
    lot_prices: tuple[Decimal, ...]
    stop_basis: Decimal | None
    profit_tracking_active: bool
    current_favorable_points: Decimal | None
    max_favorable_points: Decimal | None
    retracement_ratio: Decimal | None
    signal_kind: SignalKind | None
    rule: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _EvalContext:
    now: Timestamp
    bar: Bar | None
    trigger: str
    data_reliable: bool
    has_active_order: bool
    position_state_uncertain: bool
    candle_color: CandleColor | None
    ma_current: Decimal | None
    ma_previous: Decimal | None
    ma_slope: MaSlope
    ma_sample_count: int
    ma_range: Decimal | None
    ma_is_choppy: bool
    entry_gate_open: bool


class StrategySignalEngine:
    """One instance tracks one (instrument, contract)'s position lifecycle. Pure/no I/O
    — every input is an explicit method argument; every output is a returned
    `StrategyDecision`. Deterministic: the same call sequence always produces the same
    decisions, which is what makes this independently testable with fixed K-bar/fill/
    clock fixtures."""

    def __init__(
        self,
        *,
        instrument: Instrument,
        contract: ContractMonth,
        config: EngineConfig | None = None,
    ) -> None:
        self._instrument = instrument
        self._contract = contract
        self._config = config or EngineConfig()
        self._closes: list[Decimal] = []
        self._streak = CandleStreakCounter()
        self._last_close: Decimal | None = None
        self._last_bar_start: Timestamp | None = None
        self._position_side = PositionSide.FLAT
        self._lots: list[LotFill] = []
        self._closed_quantity = 0
        self._active_basis: Decimal | None = None
        self._profit_activated = False
        self._max_favorable: Decimal | None = None

    # -- Position introspection (read-only; for callers/tests) --------------------------

    @property
    def position_side(self) -> PositionSide:
        return self._position_side

    @property
    def lots(self) -> tuple[LotFill, ...]:
        return tuple(self._lots)

    # -- Inputs ---------------------------------------------------------------------------

    def on_bar_closed(
        self,
        bar: Bar,
        *,
        data_reliable: bool,
        has_active_order: bool,
        position_state_uncertain: bool,
    ) -> StrategyDecision:
        if bar.instrument != self._instrument or bar.contract != self._contract:
            raise InvalidStrategyEngineError(
                f"bar is for {bar.instrument.value}/{bar.contract.code}, "
                f"engine tracks {self._instrument.value}/{self._contract.code}"
            )
        self._streak.on_bar_closed(bar)
        self._closes.append(bar.close.amount)
        max_history = self._config.ma_window + self._config.flat_lookback + 5
        if len(self._closes) > max_history:
            self._closes = self._closes[-max_history:]
        self._last_close = bar.close.amount
        self._last_bar_start = bar.start
        return self._evaluate(
            now=bar.end,
            bar=bar,
            data_reliable=data_reliable,
            has_active_order=has_active_order,
            position_state_uncertain=position_state_uncertain,
            trigger="bar_closed",
        )

    def on_clock_tick(
        self,
        now: Timestamp,
        *,
        has_active_order: bool,
        position_state_uncertain: bool,
    ) -> StrategyDecision:
        return self._evaluate(
            now=now,
            bar=None,
            data_reliable=True,
            has_active_order=has_active_order,
            position_state_uncertain=position_state_uncertain,
            trigger="clock_tick",
        )

    def on_fill_confirmed(
        self, *, side: Side, price: Decimal, quantity: int, at: Timestamp
    ) -> None:
        """Only ever called with an actual broker fill report — never an order ack or
        submitted intent (line 23: "第 1 口必須以實際成交回報確認建立")."""
        if quantity <= 0:
            raise InvalidStrategyEngineError(f"quantity must be > 0, got {quantity}")
        if self._position_side is PositionSide.FLAT or self._is_opening_side(side):
            if quantity != 1:
                raise InvalidStrategyEngineError("opening fills must be exactly 1 lot at a time")
            if len(self._lots) >= self._config.max_lots:
                raise InvalidStrategyEngineError("cannot open beyond max_lots")
            if self._position_side is PositionSide.FLAT:
                self._position_side = PositionSide.LONG if side is Side.BUY else PositionSide.SHORT
            self._lots.append(LotFill(price=price, at=at))
            self._active_basis = price
        else:
            self._closed_quantity += quantity
            if self._closed_quantity > len(self._lots):
                raise InvalidStrategyEngineError("closing fill exceeds held lots")
            if self._closed_quantity == len(self._lots):
                self._reset_position()

    def _is_opening_side(self, side: Side) -> bool:
        if self._position_side is PositionSide.LONG:
            return side is Side.BUY
        if self._position_side is PositionSide.SHORT:
            return side is Side.SELL
        return True

    def _reset_position(self) -> None:
        """Only ever called once the closing quantity exactly matches the held lot
        count — "全平完成後才可清除該持倉週期的啟動狀態與最大有利點數"."""
        self._position_side = PositionSide.FLAT
        self._lots = []
        self._closed_quantity = 0
        self._active_basis = None
        self._profit_activated = False
        self._max_favorable = None

    # -- Evaluation -------------------------------------------------------------------------

    def _evaluate(
        self,
        *,
        now: Timestamp,
        bar: Bar | None,
        data_reliable: bool,
        has_active_order: bool,
        position_state_uncertain: bool,
        trigger: str,
    ) -> StrategyDecision:
        ma_values = moving_average_series(
            self._closes, self._config.ma_window, self._config.flat_lookback
        )
        ma_current = ma_values[-1] if ma_values else None
        ma_previous = ma_values[-2] if len(ma_values) >= 2 else None
        ma_range = (
            recent_range(ma_values[-self._config.flat_lookback :])
            if len(ma_values) >= self._config.flat_lookback
            else None
        )
        ctx = _EvalContext(
            now=now,
            bar=bar,
            trigger=trigger,
            data_reliable=data_reliable,
            has_active_order=has_active_order,
            position_state_uncertain=position_state_uncertain,
            candle_color=bar.candle_color if bar is not None else None,
            ma_current=ma_current,
            ma_previous=ma_previous,
            ma_slope=determine_slope(ma_current, ma_previous),
            ma_sample_count=len(self._closes),
            ma_range=ma_range,
            ma_is_choppy=is_choppy(
                ma_values,
                lookback=self._config.flat_lookback,
                threshold=self._config.flat_threshold_points,
            ),
            entry_gate_open=self._is_entry_window(now),
        )

        decision = self._check_eod(ctx)
        if decision is not None:
            return decision
        decision = self._check_stop_loss(ctx)
        if decision is not None:
            return decision
        decision = self._check_profit_pullback(ctx)
        if decision is not None:
            return decision
        return self._check_entry(ctx)

    def _is_entry_window(self, now: Timestamp) -> bool:
        t = now.value.time()
        return t < self._config.eod_flatten_local_time or t >= self._config.entry_gate_local_time

    # -- Priority 1: 04:55 forced flatten / emergency risk -----------------------------------

    def _check_eod(self, ctx: _EvalContext) -> StrategyDecision | None:
        if self._position_side is PositionSide.FLAT:
            return None
        t = ctx.now.value.time()
        in_band = self._config.eod_flatten_local_time <= t < self._config.entry_gate_local_time
        if not in_band:
            return None
        if ctx.has_active_order:
            return self._make(
                ctx,
                rule="eod_flatten",
                passed=False,
                signal_kind=None,
                reason=(
                    "04:55 強制平倉觸發，但已有活動委託，交由 order/reconciliation/risk "
                    "workflow 安全協調"
                ),
            )
        return self._make(
            ctx,
            rule="eod_flatten",
            passed=True,
            signal_kind=SignalKind.EXIT_ALL,
            reason="04:55 強制平倉",
        )

    # -- Priority 2: 300-point stop-loss ------------------------------------------------------

    def _check_stop_loss(self, ctx: _EvalContext) -> StrategyDecision | None:
        if self._position_side is PositionSide.FLAT or self._active_basis is None:
            return None
        if self._last_close is None:
            return None
        points = self._signed_points(self._last_close, self._active_basis)
        if points > -self._config.stop_loss_points:
            return None
        if ctx.has_active_order:
            return self._make(
                ctx,
                rule="stop_loss",
                passed=False,
                signal_kind=None,
                reason=f"停損已觸發（逆向 {-points} 點），但已有活動委託",
            )
        return self._make(
            ctx,
            rule="stop_loss",
            passed=True,
            signal_kind=SignalKind.EXIT_ALL,
            reason=f"停損觸發：逆向達 {-points} 點（基準 {self._active_basis}）",
        )

    # -- Priority 3: 30% profit-pullback exit -------------------------------------------------

    def _check_profit_pullback(self, ctx: _EvalContext) -> StrategyDecision | None:
        if self._position_side is PositionSide.FLAT or self._active_basis is None:
            return None
        if self._last_close is None:
            return None
        points = self._signed_points(self._last_close, self._active_basis)
        if not self._profit_activated:
            if points < self._config.profit_activation_points:
                return None
            self._profit_activated = True
            self._max_favorable = points
        elif self._max_favorable is None or points > self._max_favorable:
            self._max_favorable = points

        assert self._max_favorable is not None and self._max_favorable > 0
        retracement_ratio = (self._max_favorable - points) / self._max_favorable
        if retracement_ratio < self._config.pullback_ratio:
            return None
        if ctx.has_active_order:
            return self._make(
                ctx,
                rule="profit_pullback",
                passed=False,
                signal_kind=None,
                reason=f"獲利回吐達 {retracement_ratio:.2%}，但已有活動委託",
            )
        return self._make(
            ctx,
            rule="profit_pullback",
            passed=True,
            signal_kind=SignalKind.EXIT_ALL,
            reason=(
                f"獲利回吐觸發：最大有利點數 {self._max_favorable} 回吐 {retracement_ratio:.2%}"
            ),
        )

    # -- Priority 4: entry / add-on -----------------------------------------------------------

    def _check_entry(self, ctx: _EvalContext) -> StrategyDecision:
        if ctx.bar is None:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="非K棒觸發，無新收盤資料",
            )
        if not ctx.data_reliable:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="行情資料不可靠（stale/gap/修訂未解決），禁止新進場或加碼",
            )
        if ctx.ma_current is None:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="20MA樣本不足（未滿20根）",
            )
        if ctx.ma_is_choppy:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason=(
                    f"均線走平（近5根20MA幅度 {ctx.ma_range} < "
                    f"{self._config.flat_threshold_points}）"
                ),
            )
        if not ctx.entry_gate_open:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="位於 04:55-10:45 禁入時段",
            )
        if ctx.position_state_uncertain:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="持倉狀態不確定（券商與本機不一致），禁止新倉",
            )
        if ctx.has_active_order:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="已有活動委託，禁止新進場或加碼",
            )

        if self._position_side is PositionSide.FLAT:
            return self._check_flat_entry(ctx)
        if len(self._lots) >= self._config.max_lots:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="已達最大口數，禁止加碼",
            )
        if len(self._lots) != 1:
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="第1口尚未以實際成交回報確認，禁止加碼",
            )
        return self._check_add_on(ctx)

    def _check_flat_entry(self, ctx: _EvalContext) -> StrategyDecision:
        if self._streak.color is CandleColor.RED and self._streak.length >= 2:
            if ctx.ma_slope is MaSlope.UP:
                return self._make(
                    ctx,
                    rule="enter_long",
                    passed=True,
                    signal_kind=SignalKind.ENTER_LONG,
                    reason="連續兩根紅K且20MA向上",
                )
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="連續兩根紅K，但20MA非向上",
            )
        if self._streak.color is CandleColor.BLACK and self._streak.length >= 2:
            if ctx.ma_slope is MaSlope.DOWN:
                return self._make(
                    ctx,
                    rule="enter_short",
                    passed=True,
                    signal_kind=SignalKind.ENTER_SHORT,
                    reason="連續兩根黑K且20MA向下",
                )
            return self._make(
                ctx,
                rule="no_signal",
                passed=False,
                signal_kind=None,
                reason="連續兩根黑K，但20MA非向下",
            )
        return self._make(
            ctx, rule="no_signal", passed=False, signal_kind=None, reason="未滿足連續兩根同色K"
        )

    def _check_add_on(self, ctx: _EvalContext) -> StrategyDecision:
        if self._position_side is PositionSide.LONG:
            if ctx.candle_color is CandleColor.RED:
                return self._make(
                    ctx,
                    rule="add_long",
                    passed=True,
                    signal_kind=SignalKind.ADD_LONG,
                    reason="多單加碼：再一根紅K",
                )
            return self._make(
                ctx, rule="no_signal", passed=False, signal_kind=None, reason="非紅K，不加碼"
            )
        if ctx.candle_color is CandleColor.BLACK:
            return self._make(
                ctx,
                rule="add_short",
                passed=True,
                signal_kind=SignalKind.ADD_SHORT,
                reason="空單加碼：再一根黑K",
            )
        return self._make(
            ctx, rule="no_signal", passed=False, signal_kind=None, reason="非黑K，不加碼"
        )

    # -- Shared helpers -----------------------------------------------------------------------

    def _signed_points(self, current_price: Decimal, basis: Decimal) -> Decimal:
        if self._position_side is PositionSide.LONG:
            return current_price - basis
        if self._position_side is PositionSide.SHORT:
            return basis - current_price
        raise InvalidStrategyEngineError("no active position to compute points for")

    def _current_points_or_none(self) -> Decimal | None:
        if self._position_side is PositionSide.FLAT or self._active_basis is None:
            return None
        if self._last_close is None:
            return None
        return self._signed_points(self._last_close, self._active_basis)

    def _current_retracement_or_none(self) -> Decimal | None:
        if not self._profit_activated or self._max_favorable is None:
            return None
        points = self._current_points_or_none()
        if points is None:
            return None
        return (self._max_favorable - points) / self._max_favorable

    def _contract_key(self) -> str:
        return f"{self._instrument.value}:{self._contract.code}"

    def _bucket_for(self, rule: str, ctx: _EvalContext) -> str:
        if rule == "eod_flatten":
            return ctx.now.value.date().isoformat()
        if rule in ("stop_loss", "profit_pullback"):
            anchor = self._last_bar_start if self._last_bar_start is not None else ctx.now
            return anchor.value.isoformat()
        if ctx.bar is not None:
            return ctx.bar.start.value.isoformat()
        return ctx.now.value.isoformat()

    def _make(
        self,
        ctx: _EvalContext,
        *,
        rule: str,
        passed: bool,
        signal_kind: SignalKind | None,
        reason: str,
    ) -> StrategyDecision:
        bucket = self._bucket_for(rule, ctx)
        decision_id = f"{self._contract_key()}:{rule}:{bucket}:{ctx.trigger}"
        intent_key = f"{self._contract_key()}:{rule}:{bucket}" if signal_kind is not None else None
        return StrategyDecision(
            decision_id=decision_id,
            intent_key=intent_key,
            strategy_version=STRATEGY_VERSION,
            at=ctx.now,
            trigger=ctx.trigger,
            bar_start=ctx.bar.start if ctx.bar is not None else None,
            bar_end=ctx.bar.end if ctx.bar is not None else None,
            current_price=self._last_close,
            candle_color=ctx.candle_color,
            streak_color=self._streak.color,
            streak_length=self._streak.length,
            ma_value=ctx.ma_current,
            ma_previous_value=ctx.ma_previous,
            ma_slope=ctx.ma_slope,
            ma_sample_count=ctx.ma_sample_count,
            ma_recent_range=ctx.ma_range,
            ma_is_choppy=ctx.ma_is_choppy,
            entry_gate_open=ctx.entry_gate_open,
            data_reliable=ctx.data_reliable,
            has_active_order=ctx.has_active_order,
            position_state_uncertain=ctx.position_state_uncertain,
            position_side=self._position_side,
            position_lots=len(self._lots),
            lot_prices=tuple(lot.price for lot in self._lots),
            stop_basis=self._active_basis,
            profit_tracking_active=self._profit_activated,
            current_favorable_points=self._current_points_or_none(),
            max_favorable_points=self._max_favorable,
            retracement_ratio=self._current_retracement_or_none(),
            signal_kind=signal_kind,
            rule=rule,
            passed=passed,
            reason=reason,
        )


__all__ = [
    "STRATEGY_VERSION",
    "EngineConfig",
    "LotFill",
    "PositionSide",
    "StrategyDecision",
    "StrategySignalEngine",
]
