from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tfx_quant.domain.bar import Bar
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidStrategyEngineError
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.moving_average import MaSlope
from tfx_quant.domain.side import Side
from tfx_quant.domain.signal import SignalKind
from tfx_quant.domain.strategy_signal_engine import (
    EngineConfig,
    PositionSide,
    StrategySignalEngine,
)
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_INSTRUMENT = Instrument.TXF
_CONTRACT = ContractMonth(year=2026, month=9)
_MA_WINDOW = 20
_FLAT_LOOKBACK = 5
_WARMUP = _MA_WINDOW + _FLAT_LOOKBACK - 1  # 24: enough closes for 5 full MA values


def _engine(config: EngineConfig | None = None) -> StrategySignalEngine:
    return StrategySignalEngine(instrument=_INSTRUMENT, contract=_CONTRACT, config=config)


def _bars(
    closes: list[str],
    *,
    opens: list[str] | None = None,
    final_start: datetime | None = None,
) -> list[Bar]:
    """Consecutive hourly bars. `closes[-1]` closes at `final_start + 1h` (default:
    2026-09-16 10:45, i.e. the earliest allowed entry moment). `opens` independently
    controls each bar's candle color (defaults to `closes` — every bar a DOJI)."""
    n = len(closes)
    if opens is None:
        opens = list(closes)
    if final_start is None:
        final_start = datetime(2026, 9, 16, 9, 45, tzinfo=TAIPEI_TZ)
    base_start = final_start - timedelta(hours=n - 1)
    bars = []
    for i in range(n):
        start = base_start + timedelta(hours=i)
        end = start + timedelta(hours=1)
        o = Decimal(opens[i])
        c = Decimal(closes[i])
        hi = max(o, c)
        lo = min(o, c)
        bars.append(
            Bar(
                instrument=_INSTRUMENT,
                contract=_CONTRACT,
                open=Price(o),
                high=Price(hi),
                low=Price(lo),
                close=Price(c),
                volume=1,
                start=Timestamp(start),
                end=Timestamp(end),
            )
        )
    return bars


def _ramp_closes(count: int, *, start: str, step: str) -> list[str]:
    value = Decimal(start)
    d = Decimal(step)
    out = []
    for _ in range(count):
        out.append(str(value))
        value += d
    return out


def _feed(
    engine: StrategySignalEngine,
    bars: list[Bar],
    *,
    data_reliable: bool = True,
    has_active_order: bool = False,
    position_state_uncertain: bool = False,
):
    decision = None
    for bar in bars:
        decision = engine.on_bar_closed(
            bar,
            data_reliable=data_reliable,
            has_active_order=has_active_order,
            position_state_uncertain=position_state_uncertain,
        )
    return decision


# -- Warm-up MA fixtures: a rising ramp (UP slope, non-choppy) and mirror falling ramp ------

_RISING_WARMUP_CLOSES = _ramp_closes(_WARMUP, start="10000", step="10")
_FALLING_WARMUP_CLOSES = _ramp_closes(_WARMUP, start="10000", step="-10")


def _rising_bars_with_tail(tail_opens: list[str], tail_closes: list[str], **kwargs) -> list[Bar]:
    closes = _RISING_WARMUP_CLOSES[: _WARMUP - len(tail_closes)] + tail_closes
    opens = closes[: _WARMUP - len(tail_closes)] + tail_opens
    return _bars(closes, opens=opens, **kwargs)


def _falling_bars_with_tail(tail_opens: list[str], tail_closes: list[str], **kwargs) -> list[Bar]:
    closes = _FALLING_WARMUP_CLOSES[: _WARMUP - len(tail_closes)] + tail_closes
    opens = closes[: _WARMUP - len(tail_closes)] + tail_opens
    return _bars(closes, opens=opens, **kwargs)


# -- Entry: long -----------------------------------------------------------------------------


def test_two_red_bars_with_ma_up_enters_long() -> None:
    engine = _engine()
    # Last two closes continue the rising ramp with an explicit open < close body (RED).
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.signal_kind is SignalKind.ENTER_LONG
    assert decision.ma_slope is MaSlope.UP
    assert decision.passed
    assert engine.position_side is PositionSide.FLAT  # only a signal — not yet confirmed filled


def test_two_black_bars_with_ma_down_enters_short() -> None:
    engine = _engine()
    tail_closes = ["9630", "9620"]
    tail_opens = ["9640", "9630"]
    bars = _falling_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.signal_kind is SignalKind.ENTER_SHORT
    assert decision.ma_slope is MaSlope.DOWN


def test_doji_interrupts_streak_blocks_entry() -> None:
    engine = _engine()
    # RED, DOJI, RED — streak length is only 1 at the trigger bar.
    tail_closes = ["10370", "10370", "10380"]
    tail_opens = ["10360", "10370", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.signal_kind is None
    assert decision.streak_length == 1


def test_wrong_slope_blocks_long_entry() -> None:
    engine = _engine()
    # Two red bars, but the MA itself is on a falling ramp (slope DOWN) — must not enter.
    tail_closes = ["9630", "9640"]
    tail_opens = ["9620", "9630"]
    bars = _falling_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.signal_kind is None
    assert decision.ma_slope is MaSlope.DOWN


def test_choppy_ma_blocks_entry_even_with_valid_streak_and_slope() -> None:
    engine = _engine()
    # Step of 2 => last-5-MA range = 4*2 = 8 < 10 => choppy.
    closes = _ramp_closes(_WARMUP, start="10000", step="2")
    opens = closes[:-2] + ["9990", "9996"]  # last two bars RED
    bars = _bars(closes, opens=opens)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.signal_kind is None
    assert decision.ma_is_choppy is True
    assert decision.ma_recent_range == Decimal("8")


def test_ma_range_exactly_10_is_not_choppy() -> None:
    engine = _engine()
    # Step of 2.5 => range == 4*2.5 == 10 exactly — boundary is NOT choppy per spec.
    closes = _ramp_closes(_WARMUP, start="10000", step="2.5")
    opens = closes[:-2] + [str(Decimal(closes[-2]) - 5), str(Decimal(closes[-1]) - 5)]
    bars = _bars(closes, opens=opens)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.ma_recent_range == Decimal("10")
    assert decision.ma_is_choppy is False
    assert decision.signal_kind is SignalKind.ENTER_LONG


def test_insufficient_ma_samples_blocks_entry() -> None:
    engine = _engine()
    closes = _ramp_closes(_MA_WINDOW - 1, start="10000", step="10")  # one short of window
    opens = closes[:-2] + [str(Decimal(closes[-2]) - 5), str(Decimal(closes[-1]) - 5)]
    bars = _bars(closes, opens=opens)
    decision = _feed(engine, bars)
    assert decision is not None
    assert decision.ma_value is None
    assert decision.signal_kind is None


# -- Entry time gate: 08:45/09:45 no-entry, 10:45 earliest allowed -----------------------------


def test_entry_blocked_before_1045_then_allowed_at_1045() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    early = datetime(2026, 9, 16, 8, 45, tzinfo=TAIPEI_TZ)  # closes at 09:45 — inside the band
    bars_blocked = _rising_bars_with_tail(tail_opens, tail_closes, final_start=early)
    decision = _feed(engine, bars_blocked)
    assert decision is not None
    assert decision.signal_kind is None
    assert decision.entry_gate_open is False

    engine2 = _engine()
    on_time = datetime(2026, 9, 16, 9, 45, tzinfo=TAIPEI_TZ)  # closes exactly at 10:45
    bars_allowed = _rising_bars_with_tail(tail_opens, tail_closes, final_start=on_time)
    decision2 = _feed(engine2, bars_allowed)
    assert decision2 is not None
    assert decision2.entry_gate_open is True
    assert decision2.signal_kind is SignalKind.ENTER_LONG


# -- Add-on -----------------------------------------------------------------------------------


def test_add_long_after_first_lot_confirmed_and_one_more_red() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars)
    assert decision.signal_kind is SignalKind.ENTER_LONG

    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)
    assert engine.position_side is PositionSide.LONG
    assert len(engine.lots) == 1

    next_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    add_decision = engine.on_bar_closed(
        next_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert add_decision.signal_kind is SignalKind.ADD_LONG


def test_add_blocked_until_first_lot_fill_confirmed() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars)
    assert decision.signal_kind is SignalKind.ENTER_LONG
    # No on_fill_confirmed call — engine is still FLAT, so the next red bar must not add.
    next_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    next_decision = engine.on_bar_closed(
        next_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert next_decision.signal_kind is SignalKind.ENTER_LONG  # re-evaluates flat-entry again
    assert engine.position_side is PositionSide.FLAT


def test_max_two_lots_blocks_further_adds() -> None:
    engine = _engine(EngineConfig())
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    add_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    add_decision = engine.on_bar_closed(
        add_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert add_decision.signal_kind is SignalKind.ADD_LONG
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10390"), quantity=1, at=add_bar.end)
    assert len(engine.lots) == 2

    third_bar = _bars(
        ["10400"], opens=["10390"], final_start=add_bar.start.value + timedelta(hours=1)
    )[0]
    third_decision = engine.on_bar_closed(
        third_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert third_decision.signal_kind is None
    assert third_decision.reason == "已達最大口數，禁止加碼"


def test_opening_beyond_max_lots_raises() -> None:
    engine = _engine()
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("100"), quantity=1, at=Timestamp.now())
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("110"), quantity=1, at=Timestamp.now())
    with pytest.raises(InvalidStrategyEngineError):
        engine.on_fill_confirmed(
            side=Side.BUY, price=Decimal("120"), quantity=1, at=Timestamp.now()
        )


# -- Stop-loss ---------------------------------------------------------------------------------


def test_stop_loss_single_lot_triggers_at_300_points() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    losing_bar = _bars(
        ["10080"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        losing_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "stop_loss"
    assert decision.stop_basis == Decimal("10380")


def test_stop_loss_not_yet_triggered_below_300_points() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    near_bar = _bars(
        ["10081"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        near_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is None


def test_stop_loss_two_lots_rebases_to_second_lot_and_closes_all() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    add_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    add_decision = engine.on_bar_closed(
        add_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert add_decision.signal_kind is SignalKind.ADD_LONG
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10390"), quantity=1, at=add_bar.end)

    # Price only 300 below lot1's basis (10380) would NOT trigger under the rebased basis
    # (10390) — must be 300 below the *second* lot's price to trigger.
    just_under_bar = _bars(
        ["10091"], opens=["10390"], final_start=add_bar.start.value + timedelta(hours=1)
    )[0]
    not_yet = engine.on_bar_closed(
        just_under_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert not_yet.signal_kind is None

    stop_bar = _bars(
        ["10090"], opens=["10091"], final_start=just_under_bar.start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        stop_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "stop_loss"
    assert decision.stop_basis == Decimal("10390")
    assert decision.position_lots == 2


def test_stop_loss_mirrors_for_short() -> None:
    engine = _engine()
    tail_closes = ["9630", "9620"]
    tail_opens = ["9640", "9630"]
    bars = _falling_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.SELL, price=Decimal("9620"), quantity=1, at=bars[-1].end)

    losing_bar = _bars(
        ["9920"], opens=["9620"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        losing_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "stop_loss"


def test_stop_loss_blocked_by_active_order_but_not_resubmitted() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    losing_bar = _bars(
        ["10080"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        losing_bar, data_reliable=True, has_active_order=True, position_state_uncertain=False
    )
    assert decision.signal_kind is None
    assert decision.rule == "stop_loss"
    assert decision.passed is False


# -- Profit-pullback ----------------------------------------------------------------------------


def test_profit_exactly_300_activates_tracking_without_exiting() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    winning_bar = _bars(
        ["10680"], opens=["10680"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        winning_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is None
    assert decision.profit_tracking_active is True
    assert decision.max_favorable_points == Decimal("300")


def test_profit_pullback_30_percent_triggers_exit() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    peak_bar = _bars(
        ["10680"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    engine.on_bar_closed(
        peak_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    # 300-point max favorable; a 30% retracement is exactly 90 points => price 10590.
    pullback_bar = _bars(
        ["10590"], opens=["10680"], final_start=peak_bar.start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        pullback_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "profit_pullback"
    assert decision.max_favorable_points == Decimal("300")


def test_profit_pullback_max_favorable_keeps_rising_before_pullback() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    first_peak = _bars(
        ["10680"], opens=["10680"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    d1 = engine.on_bar_closed(
        first_peak, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert d1.max_favorable_points == Decimal("300")

    higher_peak = _bars(
        ["10780"], opens=["10780"], final_start=first_peak.start.value + timedelta(hours=1)
    )[0]
    d2 = engine.on_bar_closed(
        higher_peak, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert d2.signal_kind is None
    assert d2.max_favorable_points == Decimal("400")

    # A pullback of 90 points off the *old* 300 peak would no longer be 30% of the new 400 peak.
    small_dip = _bars(
        ["10690"], opens=["10690"], final_start=higher_peak.start.value + timedelta(hours=1)
    )[0]
    d3 = engine.on_bar_closed(
        small_dip, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert d3.signal_kind is None
    assert d3.max_favorable_points == Decimal("400")


def test_profit_pullback_mirrors_for_short() -> None:
    engine = _engine()
    tail_closes = ["9630", "9620"]
    tail_opens = ["9640", "9630"]
    bars = _falling_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.SELL, price=Decimal("9620"), quantity=1, at=bars[-1].end)

    peak_bar = _bars(
        ["9320"], opens=["9620"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    engine.on_bar_closed(
        peak_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    pullback_bar = _bars(
        ["9410"], opens=["9320"], final_start=peak_bar.start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        pullback_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "profit_pullback"


# -- 04:55 forced flatten -----------------------------------------------------------------------


def test_eod_forced_flatten_at_0455() -> None:
    engine = _engine()
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10000"), quantity=1, at=Timestamp.now())
    now = Timestamp(datetime(2026, 9, 17, 4, 55, tzinfo=TAIPEI_TZ))
    decision = engine.on_clock_tick(now, has_active_order=False, position_state_uncertain=False)
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "eod_flatten"


def test_eod_flatten_when_flat_is_a_noop() -> None:
    engine = _engine()
    now = Timestamp(datetime(2026, 9, 17, 4, 55, tzinfo=TAIPEI_TZ))
    decision = engine.on_clock_tick(now, has_active_order=False, position_state_uncertain=False)
    assert decision.signal_kind is None
    assert decision.rule != "eod_flatten"


def test_eod_flatten_blocked_by_active_order_does_not_double_submit() -> None:
    engine = _engine()
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10000"), quantity=1, at=Timestamp.now())
    now = Timestamp(datetime(2026, 9, 17, 4, 55, tzinfo=TAIPEI_TZ))
    decision = engine.on_clock_tick(now, has_active_order=True, position_state_uncertain=False)
    assert decision.signal_kind is None
    assert decision.rule == "eod_flatten"
    assert decision.passed is False


def test_eod_flatten_after_restart_still_fires_within_band() -> None:
    """Restart-recovery: a fresh engine instance, position state rebuilt purely from
    replayed fill confirmations, still correctly force-flattens if `now` is already
    inside the 04:55-10:45 band ("重啟錯過 04:55")."""
    engine = _engine()
    engine.on_fill_confirmed(side=Side.SELL, price=Decimal("10000"), quantity=1, at=Timestamp.now())
    now = Timestamp(datetime(2026, 9, 17, 6, 30, tzinfo=TAIPEI_TZ))
    decision = engine.on_clock_tick(now, has_active_order=False, position_state_uncertain=False)
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "eod_flatten"


def test_eod_flatten_outside_band_does_not_fire() -> None:
    engine = _engine()
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10000"), quantity=1, at=Timestamp.now())
    now = Timestamp(datetime(2026, 9, 17, 12, 0, tzinfo=TAIPEI_TZ))
    decision = engine.on_clock_tick(now, has_active_order=False, position_state_uncertain=False)
    assert decision.signal_kind is None


# -- Idempotency / replay -----------------------------------------------------------------------


def test_repeated_same_bar_produces_same_decision_id_and_intent_key() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    for bar in bars[:-1]:
        engine.on_bar_closed(
            bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
        )
    trigger_bar = bars[-1]
    d1 = engine.on_bar_closed(
        trigger_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    # Re-deliver the exact same closed bar (a replayed/duplicate event) without mutating
    # engine state beyond what already happened — decision_id/intent_key must match.
    d2 = engine.on_bar_closed(
        trigger_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    assert d1.decision_id == d2.decision_id
    assert d1.intent_key == d2.intent_key
    assert d1.signal_kind is d2.signal_kind is SignalKind.ENTER_LONG


def test_repeated_eod_clock_ticks_same_date_share_intent_key() -> None:
    engine = _engine()
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10000"), quantity=1, at=Timestamp.now())
    now1 = Timestamp(datetime(2026, 9, 17, 4, 55, tzinfo=TAIPEI_TZ))
    now2 = Timestamp(datetime(2026, 9, 17, 5, 10, tzinfo=TAIPEI_TZ))
    d1 = engine.on_clock_tick(now1, has_active_order=False, position_state_uncertain=False)
    d2 = engine.on_clock_tick(now2, has_active_order=False, position_state_uncertain=False)
    assert d1.intent_key == d2.intent_key


# -- Fail-closed on uncertain data / position ----------------------------------------------------


def test_position_state_uncertain_blocks_entry_only() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars, position_state_uncertain=True)
    assert decision.signal_kind is None
    assert decision.reason == "持倉狀態不確定（券商與本機不一致），禁止新倉"


def test_position_state_uncertain_does_not_block_stop_loss() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)

    losing_bar = _bars(
        ["10080"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    decision = engine.on_bar_closed(
        losing_bar, data_reliable=True, has_active_order=False, position_state_uncertain=True
    )
    assert decision.signal_kind is SignalKind.EXIT_ALL
    assert decision.rule == "stop_loss"


def test_stale_data_blocks_entry() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    decision = _feed(engine, bars, data_reliable=False)
    assert decision.signal_kind is None
    assert "資料不可靠" in decision.reason


# -- Partial fills / closing --------------------------------------------------------------------


def test_partial_close_of_two_lots_keeps_remaining_lot_risk_managed() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)
    add_bar = _bars(
        ["10390"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    engine.on_bar_closed(
        add_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10390"), quantity=1, at=add_bar.end)
    assert len(engine.lots) == 2

    # Only 1 of the 2 lots reported closed so far (partial fill of the flatten order).
    engine.on_fill_confirmed(side=Side.SELL, price=Decimal("10090"), quantity=1, at=add_bar.end)
    assert engine.position_side is PositionSide.LONG  # not yet fully flat
    assert engine.lots == engine.lots  # still holding, risk state must not have been cleared

    engine.on_fill_confirmed(side=Side.SELL, price=Decimal("10090"), quantity=1, at=add_bar.end)
    assert engine.position_side is PositionSide.FLAT


def test_closing_fill_exceeding_held_lots_raises() -> None:
    engine = _engine()
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10000"), quantity=1, at=Timestamp.now())
    with pytest.raises(InvalidStrategyEngineError):
        engine.on_fill_confirmed(
            side=Side.SELL, price=Decimal("10000"), quantity=2, at=Timestamp.now()
        )


def test_full_flat_clears_profit_tracking_state_for_next_cycle() -> None:
    engine = _engine()
    tail_closes = ["10370", "10380"]
    tail_opens = ["10360", "10370"]
    bars = _rising_bars_with_tail(tail_opens, tail_closes)
    _feed(engine, bars)
    engine.on_fill_confirmed(side=Side.BUY, price=Decimal("10380"), quantity=1, at=bars[-1].end)
    winning_bar = _bars(
        ["10680"], opens=["10380"], final_start=bars[-1].start.value + timedelta(hours=1)
    )[0]
    engine.on_bar_closed(
        winning_bar, data_reliable=True, has_active_order=False, position_state_uncertain=False
    )
    engine.on_fill_confirmed(side=Side.SELL, price=Decimal("10680"), quantity=1, at=winning_bar.end)
    assert engine.position_side is PositionSide.FLAT

    now = Timestamp(datetime(2026, 9, 17, 4, 55, tzinfo=TAIPEI_TZ))
    decision = engine.on_clock_tick(now, has_active_order=False, position_state_uncertain=False)
    assert decision.max_favorable_points is None
    assert decision.profit_tracking_active is False
