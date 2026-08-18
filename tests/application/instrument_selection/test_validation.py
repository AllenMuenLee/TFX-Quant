from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4

from tfx_quant.application.instrument_selection.selection import ResolvedSelection
from tfx_quant.application.instrument_selection.validation import (
    check_quote_position_order_consistent,
    validate_can_open,
    validate_order_price,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry
from tfx_quant.domain.money import Price
from tfx_quant.domain.order import ClientOrderId, Order, OrderKind, TimeInForce
from tfx_quant.domain.position import Position
from tfx_quant.domain.quantity import NetPosition, Quantity
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp

_ACCOUNT = TradingAccount(branch_id="F00", account_no="9808900")
_MXF_CONTRACT = ContractMonth(year=2026, month=9)
_TXF_CONTRACT = ContractMonth(year=2026, month=9)


def _entry(**overrides: object) -> InstrumentMasterEntry:
    defaults: dict[str, object] = {
        "instrument": Instrument.MXF,
        "contract": _MXF_CONTRACT,
        "vendor_symbol": "MXFU6",
        "broker_product_code": "MXF",
        "tick_size": Decimal("1"),
        "multiplier": Decimal("50"),
        "day_session_start": time(8, 45),
        "day_session_end": time(13, 45),
        "night_session_start": None,
        "night_session_end": None,
        "expiry_date": date(2026, 9, 16),
        "tradable": True,
    }
    defaults.update(overrides)
    return InstrumentMasterEntry(**defaults)  # type: ignore[arg-type]


def _timestamp(iso: str) -> Timestamp:
    return Timestamp(datetime.fromisoformat(iso).replace(tzinfo=TAIPEI_TZ))


# -- validate_can_open --------------------------------------------------------------


def test_validate_can_open_allows_tradable_unexpired_entry() -> None:
    entry = _entry()
    assert validate_can_open(entry, as_of=_timestamp("2026-08-18T10:00:00")) is None


def test_validate_can_open_rejects_missing_entry() -> None:
    reason = validate_can_open(None, as_of=_timestamp("2026-08-18T10:00:00"))
    assert reason is not None
    assert "主檔缺漏" in reason


def test_validate_can_open_rejects_non_tradable() -> None:
    entry = _entry(tradable=False)
    reason = validate_can_open(entry, as_of=_timestamp("2026-08-18T10:00:00"))
    assert reason is not None
    assert "停止交易" in reason


def test_validate_can_open_rejects_expired_contract() -> None:
    entry = _entry(expiry_date=date(2026, 8, 1))
    reason = validate_can_open(entry, as_of=_timestamp("2026-08-18T10:00:00"))
    assert reason is not None
    assert "到期" in reason


def test_validate_can_open_allows_on_expiry_date_itself() -> None:
    entry = _entry(expiry_date=date(2026, 8, 18))
    assert validate_can_open(entry, as_of=_timestamp("2026-08-18T10:00:00")) is None


# -- validate_order_price ------------------------------------------------------------


def test_validate_order_price_accepts_exact_multiple() -> None:
    entry = _entry(tick_size=Decimal("1"))
    assert validate_order_price(entry, Price(Decimal("17500"))) is None


def test_validate_order_price_rejects_non_multiple() -> None:
    entry = _entry(tick_size=Decimal("1"))
    reason = validate_order_price(entry, Price(Decimal("17500.5")))
    assert reason is not None
    assert "跳動單位" in reason


# -- check_quote_position_order_consistent -------------------------------------------


def _selection(instrument: Instrument, contract: ContractMonth) -> ResolvedSelection:
    entry = _entry(instrument=instrument, contract=contract, broker_product_code=instrument.value)
    return ResolvedSelection(instrument=instrument, contract=contract, entry=entry)


def _position(instrument: Instrument, contract: ContractMonth, lots: int) -> Position:
    return Position(
        account=_ACCOUNT,
        instrument=instrument,
        contract=contract,
        net=NetPosition(lots=lots),
        average_price=None if lots == 0 else Price(Decimal("17500")),
        as_of=_timestamp("2026-08-18T10:00:00"),
    )


def _order(instrument: Instrument, contract: ContractMonth) -> Order:
    return Order(
        client_order_id=ClientOrderId(uuid4()),
        account=_ACCOUNT,
        instrument=instrument,
        contract=contract,
        side=Side.BUY,
        quantity=Quantity(1),
        price=Price(Decimal("17500")),
        kind=OrderKind.OPEN,
        time_in_force=TimeInForce.ROD,
    )


def test_consistent_when_nothing_selected_is_false() -> None:
    assert (
        check_quote_position_order_consistent(current=None, positions=(), open_orders=())
        is False
    )


def test_consistent_with_no_positions_or_orders() -> None:
    current = _selection(Instrument.MXF, _MXF_CONTRACT)
    assert check_quote_position_order_consistent(current=current, positions=(), open_orders=())


def test_consistent_ignores_flat_positions_of_other_contracts() -> None:
    current = _selection(Instrument.MXF, _MXF_CONTRACT)
    other = _position(Instrument.TXF, _TXF_CONTRACT, lots=0)
    assert check_quote_position_order_consistent(
        current=current, positions=(other,), open_orders=()
    )


def test_inconsistent_when_non_flat_position_is_different_contract() -> None:
    current = _selection(Instrument.MXF, _MXF_CONTRACT)
    mismatched = _position(Instrument.TXF, _TXF_CONTRACT, lots=1)
    assert not check_quote_position_order_consistent(
        current=current, positions=(mismatched,), open_orders=()
    )


def test_inconsistent_when_open_order_is_different_contract() -> None:
    current = _selection(Instrument.MXF, _MXF_CONTRACT)
    mismatched = _order(Instrument.TXF, _TXF_CONTRACT)
    assert not check_quote_position_order_consistent(
        current=current, positions=(), open_orders=(mismatched,)
    )


def test_consistent_when_everything_matches() -> None:
    current = _selection(Instrument.MXF, _MXF_CONTRACT)
    matching_position = _position(Instrument.MXF, _MXF_CONTRACT, lots=1)
    matching_order = _order(Instrument.MXF, _MXF_CONTRACT)
    assert check_quote_position_order_consistent(
        current=current, positions=(matching_position,), open_orders=(matching_order,)
    )
