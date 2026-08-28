"""FIFO realized-P&L calculation, report querying, reconciliation, and CSV export."""

from __future__ import annotations

import csv
import io
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tfx_quant.application.ports.fill_ledger_repository import (
    FillAppendOutcome,
    FillLedgerRepository,
)
from tfx_quant.domain.side import Side
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trade_report import (
    LedgerFill,
    RealizedTrade,
    ReportSummary,
    TradeReport,
    money_round,
)
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)
_D = Decimal("0")


@dataclass(slots=True)
class _Lot:
    fill: LedgerFill
    remaining: int
    fee_per_lot: Decimal
    tax_per_lot: Decimal


class TradeReportService:
    def __init__(self, repository: FillLedgerRepository) -> None:
        self._repository = repository

    def record_fill(self, fill: LedgerFill) -> FillAppendOutcome:
        outcome = self._repository.append(fill)
        log_info(
            _logger,
            "fill_ledger_append",
            fill_id=fill.fill_id,
            broker_order_no=fill.broker_order_no,
            order_correlation=fill.order_correlation,
            account=fill.masked_account,
            contract=f"{fill.instrument.value}-{fill.contract}",
            side=fill.side.value,
            quantity=fill.quantity,
            price=str(fill.price),
            trading_day=fill.trading_day.isoformat(),
            fees_complete=not fill.provisional_reasons,
            dedup_result=outcome.value,
            ledger_transaction="committed",
        )
        return outcome

    def query(
        self,
        start: date,
        end: date,
        *,
        multipliers: dict[tuple[object, object], Decimal],
        instrument: object | None = None,
        expected_broker_fill_count: int | None = None,
        now: Timestamp | None = None,
    ) -> TradeReport:
        if end < start:
            raise ValueError("end must be on or after start")
        all_fills = tuple(self._repository.list_between(start, end))
        fills = tuple(f for f in all_fills if instrument is None or f.instrument == instrument)
        warnings: list[str] = []
        if expected_broker_fill_count is not None and len(all_fills) != expected_broker_fill_count:
            difference = f"expected={expected_broker_fill_count}, actual={len(all_fills)}"
            warnings.append(f"broker_fill_count_mismatch: {difference}")
        if any(f.provisional_reasons for f in fills):
            warnings.append("one_or_more_fills_have_unknown_fees")
        trades, unmatched = _fifo_match(fills, multipliers)
        if unmatched:
            warnings.append("open_positions_excluded_from_realized_pnl")
        daily = _summaries(trades, monthly=False)
        monthly = _summaries(trades, monthly=True)
        report = TradeReport(
            generated_at=now or Timestamp.now(),
            timezone="Asia/Taipei",
            start=start,
            end=end,
            fills=fills,
            realized_trades=tuple(trades),
            daily=daily,
            monthly=monthly,
            warnings=tuple(warnings),
        )
        log_info(
            _logger,
            "trade_report_query",
            start=start.isoformat(),
            end=end.isoformat(),
            filter_instrument=str(instrument),
            row_count=len(trades),
            warnings=warnings,
            generated_at=report.generated_at.value.isoformat(),
            reconciliation_difference=(
                None
                if expected_broker_fill_count is None
                else len(all_fills) - expected_broker_fill_count
            ),
        )
        return report

    def export_csv(self, report: TradeReport) -> bytes:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["generated_at", report.generated_at.value.isoformat()])
        writer.writerow(["timezone", report.timezone])
        writer.writerow(["period", report.start.isoformat(), report.end.isoformat()])
        writer.writerow(["integrity_warnings", " | ".join(report.warnings)])
        writer.writerow([])
        writer.writerow(
            [
                "trading_day",
                "instrument",
                "contract",
                "side",
                "lots",
                "open_price",
                "close_price",
                "multiplier",
                "gross_pnl",
                "commission",
                "tax",
                "net_pnl",
                "matching",
                "open_fill_ids",
                "close_fill_id",
                "provisional",
                "provisional_reasons",
            ]
        )
        for t in report.realized_trades:
            writer.writerow(
                [
                    t.trading_day,
                    t.instrument.value,
                    str(t.contract),
                    t.side.value,
                    t.quantity,
                    t.open_price,
                    t.close_price,
                    t.multiplier,
                    t.gross_pnl,
                    t.commission,
                    t.tax,
                    t.net_pnl,
                    f"{t.matching_method}/{t.matching_version}",
                    "|".join(t.open_fill_ids),
                    t.close_fill_id,
                    t.provisional,
                    "|".join(t.provisional_reasons),
                ]
            )
        data = output.getvalue().encode("utf-8-sig")
        log_info(
            _logger,
            "trade_report_export",
            start=report.start,
            end=report.end,
            row_count=len(report.realized_trades),
            warnings=report.warnings,
            generated_at=report.generated_at.value.isoformat(),
            file_result="memory_bytes",
            byte_count=len(data),
        )
        return data


def _fifo_match(
    fills: tuple[LedgerFill, ...], multipliers: dict[tuple[object, object], Decimal]
) -> tuple[list[RealizedTrade], int]:
    positions: dict[tuple[object, object, str], deque[_Lot]] = defaultdict(deque)
    trades: list[RealizedTrade] = []
    for fill in sorted(fills, key=lambda f: (f.filled_at.value, f.fill_id)):
        key = (fill.instrument, fill.contract, fill.masked_account)
        multiplier = multipliers.get((fill.instrument, fill.contract))
        if multiplier is None or not isinstance(multiplier, Decimal) or multiplier <= 0:
            raise ValueError(
                f"positive Decimal multiplier required for {fill.instrument}/{fill.contract}"
            )
        queue = positions[key]
        remaining = fill.quantity
        while remaining and queue and queue[0].fill.side is not fill.side:
            opened = queue[0]
            qty = min(remaining, opened.remaining)
            close_fee = (fill.commission or _D) / fill.quantity * qty
            close_tax = (fill.tax or _D) / fill.quantity * qty
            fees = opened.fee_per_lot * qty + close_fee
            taxes = opened.tax_per_lot * qty + close_tax
            direction = Decimal("1") if opened.fill.side is Side.BUY else Decimal("-1")
            gross = money_round((fill.price - opened.fill.price) * direction * multiplier * qty)
            commission = money_round(fees)
            tax = money_round(taxes)
            reasons = tuple(
                dict.fromkeys((*opened.fill.provisional_reasons, *fill.provisional_reasons))
            )
            trade = RealizedTrade(
                fill.trading_day,
                fill.instrument,
                fill.contract,
                opened.fill.side,
                qty,
                opened.fill.price,
                fill.price,
                multiplier,
                gross,
                commission,
                tax,
                gross - commission - tax,
                (opened.fill.fill_id,),
                fill.fill_id,
                provisional_reasons=reasons,
            )
            trades.append(trade)
            log_info(
                _logger,
                "realized_pnl_match",
                matching_method="FIFO",
                matching_version="1",
                fill_ids=[opened.fill.fill_id, fill.fill_id],
                multiplier=str(multiplier),
                gross_pnl=str(gross),
                commission=str(commission),
                tax=str(tax),
                net_pnl=str(trade.net_pnl),
                decimal_rounding="ROUND_HALF_UP quantum=1",
                provisional_reasons=reasons,
            )
            opened.remaining -= qty
            remaining -= qty
            if opened.remaining == 0:
                queue.popleft()
        if remaining:
            queue.append(
                _Lot(
                    fill,
                    remaining,
                    (fill.commission or _D) / fill.quantity,
                    (fill.tax or _D) / fill.quantity,
                )
            )
    return trades, sum(lot.remaining for q in positions.values() for lot in q)


def _summaries(trades: list[RealizedTrade], *, monthly: bool) -> tuple[ReportSummary, ...]:
    grouped: dict[date, list[RealizedTrade]] = defaultdict(list)
    for trade in trades:
        period = trade.trading_day.replace(day=1) if monthly else trade.trading_day
        grouped[period].append(trade)
    return tuple(
        ReportSummary(
            period,
            sum((t.gross_pnl for t in rows), _D),
            sum((t.commission for t in rows), _D),
            sum((t.tax for t in rows), _D),
            sum((t.net_pnl for t in rows), _D),
            sum(t.quantity for t in rows),
            any(t.provisional for t in rows),
        )
        for period, rows in sorted(grouped.items())
    )
