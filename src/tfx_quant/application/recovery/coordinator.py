"""Startup recovery queries broker truth and never submits or cancels orders."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from tfx_quant.application.ports.order_repository import OrderRepository
from tfx_quant.application.ports.position_baseline_repository import PositionBaselineRepository
from tfx_quant.application.ports.yuanta_gateways import TradeGatewayPort
from tfx_quant.domain.fill import Fill
from tfx_quant.domain.order_state_machine import OrderReport, OrderStatus
from tfx_quant.domain.position import Position

_logger = logging.getLogger(__name__)


class RecoveryStatus(StrEnum):
    PAUSED = "PAUSED"
    READY_FOR_NEW_BASELINE = "READY_FOR_NEW_BASELINE"


class RecoveryReportStore(Protocol):
    def save_recovery_report(self, report: RecoveryReport) -> None: ...


class ActiveWorkflowSource(Protocol):
    def list_active(self) -> Sequence[object]: ...


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovery_id: str
    status: RecoveryStatus
    query_orders_ok: bool
    query_fills_ok: bool
    query_positions_ok: bool
    incomplete_workflow_count: int
    unresolved_intent_ids: tuple[str, ...]
    unknown_broker_client_order_ids: tuple[str, ...]
    position_differences: tuple[str, ...]
    errors: tuple[str, ...]
    automatic_resubmissions: int
    created_at: str

    @property
    def may_create_baseline(self) -> bool:
        return self.status is RecoveryStatus.READY_FOR_NEW_BASELINE


class RecoveryCoordinator:
    """One-shot startup gate. It has deliberately no submit/cancel dependency."""

    def __init__(
        self,
        *,
        trade_gateway: TradeGatewayPort,
        order_repository: OrderRepository,
        baseline_repository: PositionBaselineRepository,
        report_store: RecoveryReportStore,
        workflow_sources: Sequence[ActiveWorkflowSource] = (),
        unresolved_outbox: Callable[[], Sequence[tuple[str, str, str]]] = lambda: (),
    ) -> None:
        self._gateway = trade_gateway
        self._orders = order_repository
        self._baselines = baseline_repository
        self._store = report_store
        self._workflows = workflow_sources
        self._unresolved_outbox = unresolved_outbox
        self._last_report: RecoveryReport | None = None

    @property
    def trading_unlocked(self) -> bool:
        return self._last_report is not None and self._last_report.may_create_baseline

    def run(self) -> RecoveryReport:
        """Query all broker evidence. Any exception or ambiguity keeps the gate closed."""
        errors: list[str] = []
        reports: Sequence[OrderReport] = ()
        fills: Sequence[Fill] = ()
        positions: Sequence[Position] = ()
        orders_ok = fills_ok = positions_ok = False
        try:
            reports = self._gateway.query_order_reports()
            orders_ok = True
            _logger.info("recovery_broker_query_completed query=orders count=%d", len(reports))
        except Exception as exc:
            errors.append(f"orders:{type(exc).__name__}")
            _logger.exception("recovery_broker_query_failed query=orders")
        try:
            fills = self._gateway.query_fills()
            fills_ok = True
            _logger.info("recovery_broker_query_completed query=fills count=%d", len(fills))
        except Exception as exc:
            errors.append(f"fills:{type(exc).__name__}")
            _logger.exception("recovery_broker_query_failed query=fills")
        try:
            positions = self._gateway.query_positions()
            positions_ok = True
            _logger.info("recovery_broker_query_completed query=positions count=%d", len(positions))
        except Exception as exc:
            errors.append(f"positions:{type(exc).__name__}")
            _logger.exception("recovery_broker_query_failed query=positions")

        active = tuple(self._orders.list_active())
        broker_client_ids = {str(item.client_order_id.value) for item in reports}
        broker_client_ids.update(str(item.client_order_id.value) for item in fills)
        unknown_broker = tuple(
            sorted(
                client_id
                for client_id in broker_client_ids
                if self._find_local_client_order(client_id) is None
            )
        )
        unresolved = {
            str(item.local_order_id.value)
            for item in active
            if item.status in {OrderStatus.CREATED, OrderStatus.SUBMITTING, OrderStatus.UNKNOWN}
        }
        unresolved.update(row[0] for row in self._unresolved_outbox())
        workflow_count = sum(len(source.list_active()) for source in self._workflows)

        differences: list[str] = []
        if positions_ok:
            for position in positions:
                baseline = self._baselines.get(
                    position.account, position.instrument, position.contract
                )
                expected = 0 if baseline is None else baseline.expected_net.lots
                if position.net.lots != expected:
                    differences.append(
                        f"{position.instrument.value}:{position.contract.code}:"
                        f"expected={expected}:actual={position.net.lots}"
                    )
        safe = (
            orders_ok and fills_ok and positions_ok and not errors and not unresolved
            and not unknown_broker and not differences and workflow_count == 0
        )
        report = RecoveryReport(
            recovery_id=str(uuid4()),
            status=(RecoveryStatus.READY_FOR_NEW_BASELINE if safe else RecoveryStatus.PAUSED),
            query_orders_ok=orders_ok,
            query_fills_ok=fills_ok,
            query_positions_ok=positions_ok,
            incomplete_workflow_count=workflow_count,
            unresolved_intent_ids=tuple(sorted(unresolved)),
            unknown_broker_client_order_ids=unknown_broker,
            position_differences=tuple(differences),
            errors=tuple(errors),
            automatic_resubmissions=0,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._store.save_recovery_report(report)
        self._last_report = report
        _logger.warning(
            "recovery_completed recovery_id=%s status=%s unresolved=%d unknown=%d "
            "differences=%d automatic_resubmissions=0",
            report.recovery_id, report.status.value, len(unresolved), len(unknown_broker),
            len(differences),
        )
        return report

    def _find_local_client_order(self, client_id: str) -> object | None:
        """Keep UUID parsing isolated; malformed broker identifiers remain unknown."""
        from uuid import UUID

        from tfx_quant.domain.order import ClientOrderId

        try:
            return self._orders.find_by_client_order_id(ClientOrderId(UUID(client_id)))
        except (TypeError, ValueError):
            return None


class SqliteRecoveryReportStore:
    """Small adapter usable with a managed SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save_recovery_report(self, report: RecoveryReport) -> None:
        connection = self._connection
        payload = json.dumps(asdict(report), ensure_ascii=False, default=str, sort_keys=True)
        connection.execute(
            "INSERT INTO recovery_reports VALUES (?, ?, ?, ?)",
            (report.recovery_id, report.status.value, payload, report.created_at),
        )
        connection.commit()


__all__ = ["RecoveryCoordinator", "RecoveryReport", "RecoveryStatus", "SqliteRecoveryReportStore"]
