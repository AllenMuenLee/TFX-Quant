"""FillLedgerService — turns every applied broker fill into an append-only `LedgerFill`.

Subscribes to `FillReceived` and joins the matching `OrderIntent` for the context a bare
`domain.Fill` does not carry (workflow/correlation id, masked account, broker order
number, open/close effect). It never mutates order state — `OrderManager` remains the
sole owner of the order lifecycle — so it is safe regardless of subscriber order. The
composition root constructs it *after* `OrderManager` so its subscription runs second and
it always observes an intent whose `broker_order_no` is already populated (an ACK always
precedes a fill).

`record_fill` is idempotent (the ledger's primary key is the broker fill id), so a
duplicate `FillReceived` — a replayed callback, a crash-recovery re-emit — appends
nothing and produces no second realized trade.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from tfx_quant.application.events.events import (
    Event,
    FillReceived,
    TradeLedgerAppendFailed,
    TradeLedgerFillRecorded,
)
from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.trade_reports.fee_model import FillFeeModel
from tfx_quant.application.trade_reports.service import TradeReportService
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import OrderKind
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.domain.trade_report import LedgerFill, PositionEffect
from tfx_quant.telemetry import get_logger, log_info, log_warning
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)

TradingDayResolver = Callable[[Timestamp, Instrument, ContractMonth], date]
MultiplierLookup = Callable[[Instrument, ContractMonth], Decimal]


class EventBus(Protocol):
    """Structural stand-in for `EventCoordinator` — same seam as `OrderManager`'s."""

    def subscribe(
        self, event_type: type[Event], handler: Callable[[Any], None]
    ) -> Callable[[], None]: ...

    def publish(self, event: Event) -> None: ...


_EFFECT_BY_KIND = {OrderKind.OPEN: PositionEffect.OPEN, OrderKind.CLOSE: PositionEffect.CLOSE}


class FillLedgerService:
    def __init__(
        self,
        *,
        report_service: TradeReportService,
        order_repository: OrderRepository,
        trading_day_resolver: TradingDayResolver,
        multiplier_lookup: MultiplierLookup,
        fee_model: FillFeeModel,
        event_bus: EventBus,
        simulation: bool,
        source: str,
    ) -> None:
        self._reports = report_service
        self._orders = order_repository
        self._trading_day = trading_day_resolver
        self._multiplier = multiplier_lookup
        self._fee_model = fee_model
        self._bus = event_bus
        self._simulation = simulation
        self._source = source
        event_bus.subscribe(FillReceived, self._on_fill)

    @property
    def source(self) -> str:
        return self._source

    @property
    def simulation(self) -> bool:
        return self._simulation

    def _on_fill(self, event: FillReceived) -> None:
        fill = event.fill
        intent = self._orders.find_by_client_order_id(fill.client_order_id)
        if intent is None:
            self._fail(fill.at, fill.client_order_id, "no matching order intent for fill")
            return
        if intent.broker_order_no is None:
            self._fail(fill.at, fill.client_order_id, "order intent has no broker order number yet")
            return

        lots = fill.quantity.lots
        price = fill.price.amount
        multiplier = self._multiplier(fill.instrument, intent.contract)
        ledger_fill = LedgerFill(
            fill_id=fill.broker_fill_no,
            broker_order_no=intent.broker_order_no,
            order_correlation=intent.workflow_id,
            masked_account=mask_account(intent.account.account_no),
            instrument=fill.instrument,
            contract=intent.contract,
            side=fill.side,
            position_effect=_EFFECT_BY_KIND.get(intent.kind, PositionEffect.AUTO),
            quantity=lots,
            price=price,
            filled_at=fill.at,
            trading_day=self._trading_day(fill.at, fill.instrument, intent.contract),
            commission=self._fee_model.commission(lots=lots),
            tax=self._fee_model.tax(lots=lots, price=price, multiplier=multiplier),
            source=self._source,
            simulation=self._simulation,
        )

        try:
            outcome = self._reports.record_fill(ledger_fill)
        except Exception as exc:  # noqa: BLE001 - a ledger write failure must not crash the bus
            log_warning(
                _logger,
                "fill_ledger_append_failed",
                fill_id=fill.broker_fill_no,
                order_correlation=intent.workflow_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            self._bus.publish(
                TradeLedgerAppendFailed(
                    at=fill.at,
                    client_order_id=fill.client_order_id,
                    reason=f"ledger write failed: {exc}",
                )
            )
            return

        log_info(
            _logger,
            "fill_ledger_translation",
            fill_id=ledger_fill.fill_id,
            broker_order_no=mask_account(ledger_fill.broker_order_no),
            order_correlation=ledger_fill.order_correlation,
            position_effect=ledger_fill.position_effect.value,
            fee_model_version=self._fee_model.version,
            source=self._source,
            simulation=self._simulation,
            dedup_result=outcome.value,
        )
        self._bus.publish(
            TradeLedgerFillRecorded(
                at=fill.at,
                fill_id=ledger_fill.fill_id,
                outcome=outcome.value,
                order_correlation=ledger_fill.order_correlation,
                simulation=self._simulation,
            )
        )

    def _fail(self, at: Timestamp, client_order_id: Any, reason: str) -> None:
        log_warning(
            _logger,
            "fill_ledger_translation_unmatched",
            client_order_id=str(client_order_id.value),
            reason=reason,
        )
        self._bus.publish(
            TradeLedgerAppendFailed(at=at, client_order_id=client_order_id, reason=reason)
        )


__all__ = ["FillLedgerService"]
