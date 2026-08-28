from datetime import date, datetime
from decimal import Decimal

from tfx_quant.application.ports.fill_ledger_repository import FillAppendOutcome
from tfx_quant.application.trade_reports import TradeReportService
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.domain.trade_report import LedgerFill, PositionEffect
from tfx_quant.persistence.sqlite_connection import create_connection
from tfx_quant.persistence.sqlite_fill_ledger_repository import SqliteFillLedgerRepository

CONTRACT = ContractMonth(2026, 9)
MULTIPLIERS = {(Instrument.MXF, CONTRACT): Decimal("50")}


def fill(
    fill_id: str,
    side: Side,
    quantity: int,
    price: str,
    instant: datetime,
    trading_day: date,
    *,
    commission: str | None = "20",
    tax: str | None = "5",
) -> LedgerFill:
    return LedgerFill(
        fill_id=fill_id,
        broker_order_no=f"order-{fill_id}",
        order_correlation=f"corr-{fill_id}",
        masked_account="****1234",
        instrument=Instrument.MXF,
        contract=CONTRACT,
        side=side,
        position_effect=PositionEffect.AUTO,
        quantity=quantity,
        price=Decimal(price),
        filled_at=Timestamp(instant.replace(tzinfo=TAIPEI_TZ)),
        trading_day=trading_day,
        commission=None if commission is None else Decimal(commission),
        tax=None if tax is None else Decimal(tax),
        source="yuanta_fill_report",
    )


def service(tmp_path):
    connection = create_connection(tmp_path / "fills.sqlite")
    repository = SqliteFillLedgerRepository(connection)
    return TradeReportService(repository), repository


def test_fifo_partial_close_fees_daily_monthly_and_bom_export(tmp_path) -> None:
    reports, _ = service(tmp_path)
    day = date(2026, 8, 25)
    reports.record_fill(fill("open", Side.BUY, 2, "100", datetime(2026, 8, 24, 22), day))
    reports.record_fill(fill("close1", Side.SELL, 1, "110", datetime(2026, 8, 25, 1), day))
    reports.record_fill(fill("close2", Side.SELL, 1, "90", datetime(2026, 8, 25, 10), day))

    report = reports.query(day, day, multipliers=MULTIPLIERS)

    assert [trade.gross_pnl for trade in report.realized_trades] == [
        Decimal("500"),
        Decimal("-500"),
    ]
    assert report.daily[0].gross_pnl == Decimal("0")
    assert report.daily[0].commission == Decimal("60")
    # Each realized match is rounded independently using the report's audited rule.
    assert report.daily[0].tax == Decimal("16")
    assert report.daily[0].net_pnl == Decimal("-76")
    assert report.daily[0].filled_lots == 2
    assert report.monthly[0].period == date(2026, 8, 1)
    exported = reports.export_csv(report)
    assert exported.startswith(b"\xef\xbb\xbf")
    assert b"open_fill_ids" in exported


def test_short_reversal_cross_month_and_duplicate_report(tmp_path) -> None:
    reports, repository = service(tmp_path)
    august = date(2026, 8, 31)
    september = date(2026, 9, 1)
    first = fill("short", Side.SELL, 1, "100", datetime(2026, 8, 31, 10), august)
    assert reports.record_fill(first) is FillAppendOutcome.INSERTED
    assert reports.record_fill(first) is FillAppendOutcome.DUPLICATE
    reports.record_fill(fill("reverse", Side.BUY, 2, "90", datetime(2026, 9, 1, 1), september))
    reports.record_fill(fill("long-close", Side.SELL, 1, "95", datetime(2026, 9, 1, 10), september))

    report = reports.query(august, september, multipliers=MULTIPLIERS)

    assert repository.count() == 3
    assert [t.gross_pnl for t in report.realized_trades] == [Decimal("500"), Decimal("250")]
    assert len(report.monthly) == 1
    assert report.monthly[0].period == date(2026, 9, 1)
    assert report.realized_trades[0].open_fill_ids == ("short",)


def test_unknown_fees_and_reconciliation_mismatch_are_provisional(tmp_path) -> None:
    reports, _ = service(tmp_path)
    day = date(2026, 8, 25)
    reports.record_fill(
        fill("a", Side.BUY, 1, "100", datetime(2026, 8, 25, 9), day, commission=None)
    )
    reports.record_fill(fill("b", Side.SELL, 1, "101", datetime(2026, 8, 25, 10), day))

    report = reports.query(day, day, multipliers=MULTIPLIERS, expected_broker_fill_count=3)

    assert report.realized_trades[0].provisional
    assert report.daily[0].provisional
    assert "one_or_more_fills_have_unknown_fees" in report.warnings
    assert report.warnings[0].startswith("broker_fill_count_mismatch")
