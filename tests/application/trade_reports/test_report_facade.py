from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from tfx_quant.application.trade_reports.report_facade import TradeReportFacade
from tfx_quant.application.trade_reports.service import TradeReportService
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trade_report import LedgerFill, PositionEffect
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository

_CONTRACT = ContractMonth(2026, 9)
_DAY = date(2026, 8, 25)


def _fill(fill_id: str, side: Side, qty: int, price: str) -> LedgerFill:
    return LedgerFill(
        fill_id=fill_id,
        broker_order_no=f"B-{fill_id}",
        order_correlation=f"wf-{fill_id}",
        masked_account="***4567",
        instrument=Instrument.MXF,
        contract=_CONTRACT,
        side=side,
        position_effect=PositionEffect.AUTO,
        quantity=qty,
        price=Decimal(price),
        filled_at=Timestamp(datetime(2026, 8, 25, 10, tzinfo=TAIPEI_TZ)),
        trading_day=_DAY,
        commission=Decimal("0"),
        tax=Decimal("0"),
        source="SIMULATION",
        simulation=True,
    )


def _facade(
    multiplier: Decimal | None = Decimal("50"),
) -> tuple[TradeReportFacade, TradeReportService]:
    repo = SqliteFillLedgerRepository(sqlite3.connect(":memory:", check_same_thread=False))
    service = TradeReportService(repo)

    def lookup(_i: Instrument, _c: ContractMonth) -> Decimal:
        if multiplier is None:
            raise ValueError("no instrument master entry")
        return multiplier

    return TradeReportFacade(service, repo, lookup), service


def test_facade_resolves_the_mxf_multiplier_from_the_lookup() -> None:
    facade, service = _facade(Decimal("50"))
    service.record_fill(_fill("open", Side.BUY, 1, "100"))
    service.record_fill(_fill("close", Side.SELL, 1, "110"))

    report = facade.build_report(_DAY, _DAY)

    assert report.realized_trades[0].gross_pnl == Decimal("500")  # (110-100) * 50
    assert report.realized_trades[0].multiplier == Decimal("50")


def test_facade_propagates_a_missing_master_entry_as_an_error() -> None:
    facade, service = _facade(multiplier=None)
    service.record_fill(_fill("open", Side.BUY, 1, "100"))
    service.record_fill(_fill("close", Side.SELL, 1, "110"))

    with pytest.raises(ValueError, match="instrument master"):
        facade.build_report(_DAY, _DAY)


def test_facade_export_csv_delegates_and_keeps_the_bom() -> None:
    facade, service = _facade()
    service.record_fill(_fill("open", Side.BUY, 1, "100"))
    service.record_fill(_fill("close", Side.SELL, 1, "110"))

    exported = facade.export_csv(facade.build_report(_DAY, _DAY))

    assert exported.startswith(b"\xef\xbb\xbf")


def test_empty_window_needs_no_multipliers() -> None:
    facade, _service = _facade(multiplier=None)  # lookup would raise if called

    report = facade.build_report(_DAY, _DAY)

    assert report.realized_trades == ()
    assert report.fills == ()
