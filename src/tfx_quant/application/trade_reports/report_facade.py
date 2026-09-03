"""TradeReportFacade — the single entry point the UI and acceptance tests use.

`TradeReportService.query` needs a `(instrument, contract) -> multiplier` map up front
(its FIFO matcher raises if a fill's multiplier is missing). The multiplier is controlled
per-contract data that lives in the instrument master — an infrastructure concern — so
this facade takes a `multiplier_lookup` callable injected by the composition root and
resolves exactly the contracts that appear in the queried window, keeping
`application.trade_reports` free of any infrastructure import.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from tfx_quant.application.ports.fill_ledger_repository import FillLedgerRepository
from tfx_quant.application.trade_reports.service import TradeReportService
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trade_report import TradeReport

MultiplierLookup = Callable[[Instrument, ContractMonth], Decimal]


class TradeReportFacade:
    def __init__(
        self,
        report_service: TradeReportService,
        fill_ledger: FillLedgerRepository,
        multiplier_lookup: MultiplierLookup,
    ) -> None:
        self._reports = report_service
        self._fill_ledger = fill_ledger
        self._multiplier_lookup = multiplier_lookup

    def build_report(
        self,
        start: date,
        end: date,
        *,
        instrument: Instrument | None = None,
        expected_broker_fill_count: int | None = None,
        now: Timestamp | None = None,
    ) -> TradeReport:
        fills = tuple(self._fill_ledger.list_between(start, end))
        # `TradeReportService.query` keys multipliers by `(object, object)` (its FIFO
        # matcher looks them up by the fill's own instrument/contract values).
        multipliers: dict[tuple[object, object], Decimal] = {}
        for fill in fills:
            key = (fill.instrument, fill.contract)
            if key not in multipliers:
                multipliers[key] = self._multiplier_lookup(fill.instrument, fill.contract)
        return self._reports.query(
            start,
            end,
            multipliers=multipliers,
            instrument=instrument,
            expected_broker_fill_count=expected_broker_fill_count,
            now=now,
        )

    def export_csv(self, report: TradeReport) -> bytes:
        return self._reports.export_csv(report)


__all__ = ["TradeReportFacade"]
