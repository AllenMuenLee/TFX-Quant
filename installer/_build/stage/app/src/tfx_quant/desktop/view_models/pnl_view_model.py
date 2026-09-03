"""Daily / monthly realized-P&L rows — Feature 12's 損益 group.

Reads the *same* `TradeReportFacade` production uses; the only difference in the 測試環境 is that
every row's `simulation` flag is `True`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tfx_quant.application.trade_reports.report_facade import TradeReportFacade
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.trade_report import ReportSummary


@dataclass(frozen=True, slots=True)
class PnlPeriodRow:
    period: str
    gross_pnl: Decimal
    commission: Decimal
    tax: Decimal
    net_pnl: Decimal
    filled_lots: int
    provisional: bool
    simulation: bool


@dataclass(frozen=True, slots=True)
class PnlView:
    daily: tuple[PnlPeriodRow, ...]
    monthly: tuple[PnlPeriodRow, ...]
    realized_net_total: Decimal
    simulation: bool
    warnings: tuple[str, ...]


def _row(summary: ReportSummary) -> PnlPeriodRow:
    return PnlPeriodRow(
        period=summary.period.isoformat(),
        gross_pnl=summary.gross_pnl,
        commission=summary.commission,
        tax=summary.tax,
        net_pnl=summary.net_pnl,
        filled_lots=summary.filled_lots,
        provisional=summary.provisional,
        simulation=summary.simulation,
    )


def build_pnl_view(
    facade: TradeReportFacade,
    start: date,
    end: date,
    *,
    instrument: Instrument | None = None,
) -> PnlView:
    report = facade.build_report(start, end, instrument=instrument)
    return PnlView(
        daily=tuple(_row(s) for s in report.daily),
        monthly=tuple(_row(s) for s in report.monthly),
        realized_net_total=sum((s.net_pnl for s in report.daily), Decimal("0")),
        simulation=report.simulation,
        warnings=report.warnings,
    )


__all__ = ["PnlPeriodRow", "PnlView", "build_pnl_view"]
