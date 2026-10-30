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
from tfx_quant.application.strategy_signal.history_replay import HistoricalTrade
from tfx_quant.application.trade_reports.report_facade import TradeReportFacade
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order_state_machine import OrderIntent
from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.domain.trade_report import LedgerFill, RealizedTrade, TradeReport
from tfx_quant.telemetry.audit import AuditTimelineStep

AuditTimelineReader = Callable[[str], tuple[AuditTimelineStep, ...]]


@dataclass(frozen=True, slots=True)
class TradeReportView:
    report: TradeReport
    simulation: bool


@dataclass(frozen=True, slots=True)
class ExecutedTradeDetails:
    entry_signal_at: str
    entry_at: str
    exit_signal_at: str
    exit_at: str
    entry_reason: str
    exit_reason: str
    entry_reference: str
    exit_reference: str


def executed_trade_details(
    report: TradeReport,
    orders: tuple[OrderIntent, ...] | list[OrderIntent],
    candidates: tuple[HistoricalTrade, ...] | list[HistoricalTrade],
) -> dict[RealizedTrade, ExecutedTradeDetails]:
    """Join FIFO matches back to their exact fills and strategy decisions.

    Older order rows did not persist their free-text reason. Historical strategy
    decisions are deterministic, so their intent key recovers that reason after a
    restart; genuinely unavailable legacy reasons are labelled instead of shown blank.
    """
    fills = {fill.fill_id: fill for fill in report.fills}
    orders_by_broker_no = {
        order.broker_order_no: order for order in orders if order.broker_order_no is not None
    }
    orders_by_workflow = {order.workflow_id: order for order in orders}
    orders_by_key = {order.idempotency_key: order for order in orders}
    candidates_by_key = {
        candidate.decision.intent_key: candidate
        for candidate in candidates
        if candidate.decision.intent_key is not None
    }

    def reason(fill: LedgerFill | None) -> str:
        if fill is None:
            return "原因資料遺失"
        order = (
            orders_by_broker_no.get(fill.broker_order_no)
            or orders_by_workflow.get(fill.order_correlation)
            or orders_by_key.get(fill.order_correlation)
        )
        key = (fill.order_correlation if order is None else order.idempotency_key).split(
            "#retry", 1
        )[0]
        candidate = candidates_by_key.get(key)
        if candidate is not None:
            return candidate.decision.reason
        if order is not None and order.reason:
            prefix = "歷史模擬成交："
            return order.reason.removeprefix(prefix)
        return "舊交易未保存原因"

    def order_and_candidate(
        fill: LedgerFill | None,
    ) -> tuple[OrderIntent | None, HistoricalTrade | None]:
        if fill is None:
            return None, None
        order = (
            orders_by_broker_no.get(fill.broker_order_no)
            or orders_by_workflow.get(fill.order_correlation)
            or orders_by_key.get(fill.order_correlation)
        )
        key = (fill.order_correlation if order is None else order.idempotency_key).split(
            "#retry", 1
        )[0]
        return order, candidates_by_key.get(key)

    def signal_at(fill: LedgerFill | None) -> str:
        order, candidate = order_and_candidate(fill)
        at = (
            candidate.decision.at
            if candidate is not None
            else None
            if order is None
            else order.triggered_at or order.created_at
        )
        return "—" if at is None else at.value.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    def reference(fill: LedgerFill | None) -> str:
        if fill is None:
            return "—"
        order, _candidate = order_and_candidate(fill)
        order_no = fill.broker_order_no if order is None else order.broker_order_no
        return f"委託 {order_no or '—'} / 成交 {fill.fill_id}"

    result: dict[RealizedTrade, ExecutedTradeDetails] = {}
    for trade in report.realized_trades:
        entry_fills = [fills[fill_id] for fill_id in trade.open_fill_ids if fill_id in fills]
        close_fill = fills.get(trade.close_fill_id)
        entry_times = sorted(fill.filled_at.value.astimezone(TAIPEI_TZ) for fill in entry_fills)
        entry_reasons = tuple(dict.fromkeys(reason(fill) for fill in entry_fills))
        result[trade] = ExecutedTradeDetails(
            entry_signal_at=signal_at(entry_fills[0] if entry_fills else None),
            entry_at=(entry_times[0].strftime("%Y-%m-%d %H:%M:%S") if entry_times else "—"),
            exit_signal_at=signal_at(close_fill),
            exit_at=(
                close_fill.filled_at.value.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
                if close_fill is not None
                else "—"
            ),
            entry_reason="；".join(entry_reasons) if entry_reasons else "原因資料遺失",
            exit_reason=reason(close_fill),
            entry_reference="；".join(reference(fill) for fill in entry_fills) or "—",
            exit_reference=reference(close_fill),
        )
    return result


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
    "ExecutedTradeDetails",
    "TradeReportView",
    "build_trade_report_view",
    "drill_down",
    "executed_trade_details",
    "export_csv",
]
