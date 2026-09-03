"""Trade-report table + full decision→P&L drill-down — Feature 12's 交易報告 group.

The drill-down walks: `RealizedTrade` → its constituent `LedgerFill`s → the `OrderIntent`(s)
they belong to (by `order_correlation`/`workflow_id`) → the audited event timeline for
that workflow (decision, intent persist, submit, ack, fill, position change, P&L match).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.trade_reports.report_facade import TradeReportFacade
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order_state_machine import OrderIntent
from tfx_quant.domain.trade_report import LedgerFill, RealizedTrade, TradeReport
from tfx_quant.telemetry.audit import AuditTimelineStep

AuditTimelineReader = Callable[[str], tuple[AuditTimelineStep, ...]]


@dataclass(frozen=True, slots=True)
class TradeReportView:
    report: TradeReport
    simulation: bool


def build_trade_report_view(
    facade: TradeReportFacade,
    start: date,
    end: date,
    *,
    instrument: Instrument | None = None,
    direction: str | None = None,
    workflow_id: str | None = None,
) -> TradeReportView:
    report = facade.build_report(start, end, instrument=instrument)
    trades = report.realized_trades
    if direction is not None:
        trades = tuple(t for t in trades if t.side.value == direction)
    if workflow_id is not None:
        fill_ids = {f.fill_id for f in report.fills if f.order_correlation == workflow_id}
        trades = tuple(
            t for t in trades if t.close_fill_id in fill_ids or set(t.open_fill_ids) & fill_ids
        )
    filtered = TradeReport(
        generated_at=report.generated_at,
        timezone=report.timezone,
        start=report.start,
        end=report.end,
        fills=report.fills,
        realized_trades=trades,
        daily=report.daily,
        monthly=report.monthly,
        warnings=report.warnings,
        simulation=report.simulation,
    )
    return TradeReportView(report=filtered, simulation=report.simulation)


def export_csv(facade: TradeReportFacade, view: TradeReportView) -> bytes:
    return facade.export_csv(view.report)


@dataclass(frozen=True, slots=True)
class DrillDown:
    trade: RealizedTrade
    fills: tuple[LedgerFill, ...]
    order_correlations: tuple[str, ...]
    intents: tuple[OrderIntent, ...]
    timeline: tuple[AuditTimelineStep, ...]


def drill_down(
    trade: RealizedTrade,
    report: TradeReport,
    order_repository: OrderRepository,
    audit_timeline_reader: AuditTimelineReader,
) -> DrillDown:
    wanted = {trade.close_fill_id, *trade.open_fill_ids}
    fills = tuple(f for f in report.fills if f.fill_id in wanted)
    correlations = tuple(dict.fromkeys(f.order_correlation for f in fills))
    intents = tuple(i for i in order_repository.list_all() if i.workflow_id in set(correlations))
    steps: list[AuditTimelineStep] = []
    for correlation in correlations:
        steps.extend(audit_timeline_reader(correlation))
    steps.sort(key=lambda s: s.seq)
    return DrillDown(
        trade=trade,
        fills=fills,
        order_correlations=correlations,
        intents=intents,
        timeline=tuple(steps),
    )


__all__ = [
    "DrillDown",
    "TradeReportView",
    "build_trade_report_view",
    "drill_down",
    "export_csv",
]
