from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.application.recovery import RecoveryCoordinator, RecoveryStatus


class Gateway:
    def __init__(self, *, fail_positions: bool = False) -> None:
        self.fail_positions = fail_positions
        self.submit_calls = 0

    def query_order_reports(self):  # type: ignore[no-untyped-def]
        return ()

    def query_fills(self):  # type: ignore[no-untyped-def]
        return ()

    def query_positions(self):  # type: ignore[no-untyped-def]
        if self.fail_positions:
            raise ConnectionError("offline")
        return ()

    def submit_order(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.submit_calls += 1


class Orders:
    def __init__(self, active=()):  # type: ignore[no-untyped-def]
        self.active = active

    def list_active(self):  # type: ignore[no-untyped-def]
        return self.active


class Baselines:
    def get(self, account, instrument, contract):  # type: ignore[no-untyped-def]
        return None


class Store:
    def __init__(self) -> None:
        self.saved = []

    def save_recovery_report(self, report):  # type: ignore[no-untyped-def]
        self.saved.append(report)


@dataclass
class WorkflowSource:
    active: tuple[object, ...]

    def list_active(self):  # type: ignore[no-untyped-def]
        return self.active


def test_clean_queries_only_unlock_baseline_creation_and_never_submit() -> None:
    gateway = Gateway()
    store = Store()
    coordinator = RecoveryCoordinator(
        trade_gateway=gateway, order_repository=Orders(), baseline_repository=Baselines(),
        report_store=store,
    )
    report = coordinator.run()
    assert report.status is RecoveryStatus.READY_FOR_NEW_BASELINE
    assert coordinator.trading_unlocked is True
    assert report.automatic_resubmissions == 0
    assert gateway.submit_calls == 0
    assert store.saved == [report]


def test_query_failure_and_unfinished_workflow_keep_recovery_paused() -> None:
    gateway = Gateway(fail_positions=True)
    coordinator = RecoveryCoordinator(
        trade_gateway=gateway, order_repository=Orders(), baseline_repository=Baselines(),
        report_store=Store(), workflow_sources=(WorkflowSource((object(),)),),
    )
    report = coordinator.run()
    assert report.status is RecoveryStatus.PAUSED
    assert report.query_positions_ok is False
    assert report.incomplete_workflow_count == 1
    assert coordinator.trading_unlocked is False
    assert gateway.submit_calls == 0


def test_unresolved_outbox_checkpoint_never_gets_resent() -> None:
    gateway = Gateway()
    coordinator = RecoveryCoordinator(
        trade_gateway=gateway, order_repository=Orders(), baseline_repository=Baselines(),
        report_store=Store(),
        unresolved_outbox=lambda: (("outbox-1", "decision-1", "BROKER_CALL_STARTED"),),
    )
    report = coordinator.run()
    assert report.status is RecoveryStatus.PAUSED
    assert report.unresolved_intent_ids == ("outbox-1",)
    assert gateway.submit_calls == 0
