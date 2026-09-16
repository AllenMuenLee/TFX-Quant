from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tfx_quant.desktop.readiness_frame import _MAX_TIMER_DELAY_MS, ReadinessFrame
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


def _frame(now: datetime) -> SimpleNamespace:
    selection = SimpleNamespace(
        current=SimpleNamespace(
            entry=SimpleNamespace(expires_at=datetime(2026, 9, 16, 13, 30, tzinfo=TAIPEI_TZ))
        ),
        auto_refresh_error=None,
        refresh_auto_selection=MagicMock(return_value=True),
    )
    frame = SimpleNamespace(
        _services=SimpleNamespace(
            instrument_selection=selection,
            clock=SimpleNamespace(now=lambda: Timestamp(now)),
        ),
        _closing=False,
        _contract_rollover_timer=MagicMock(),
        _night_resume_timer=MagicMock(),
        _selector=MagicMock(),
        _refresh=MagicMock(),
        _unsubscribers=[MagicMock()],
    )
    frame._arm_contract_rollover = lambda: ReadinessFrame._arm_contract_rollover(frame)
    frame._apply_contract_change = lambda: ReadinessFrame._apply_contract_change(frame)
    return frame


def test_one_shot_targets_expiry_cutoff_without_recalculating() -> None:
    frame = _frame(datetime(2026, 9, 16, 13, 29, tzinfo=TAIPEI_TZ))
    frame._arm_contract_rollover()
    frame._contract_rollover_timer.StartOnce.assert_called_once_with(60_000)
    frame._services.instrument_selection.refresh_auto_selection.assert_not_called()


def test_long_wait_reschedules_without_recalculating() -> None:
    frame = _frame(datetime(2026, 6, 17, 13, 30, tzinfo=TAIPEI_TZ))
    frame._arm_contract_rollover()
    frame._contract_rollover_timer.StartOnce.assert_called_with(_MAX_TIMER_DELAY_MS)
    ReadinessFrame._on_contract_rollover_wake(frame, None)  # type: ignore[arg-type]
    frame._services.instrument_selection.refresh_auto_selection.assert_not_called()


@pytest.mark.parametrize("hour, minute", [(13, 30), (14, 45)])
def test_due_or_delayed_timer_updates_contract_once(hour: int, minute: int) -> None:
    frame = _frame(datetime(2026, 9, 16, hour, minute, tzinfo=TAIPEI_TZ))
    selection = frame._services.instrument_selection

    def rollover() -> bool:
        selection.current.entry.expires_at = datetime(2026, 12, 16, 13, 30, tzinfo=TAIPEI_TZ)
        return True

    selection.refresh_auto_selection.side_effect = rollover
    ReadinessFrame._on_contract_rollover_wake(frame, None)  # type: ignore[arg-type]
    selection.refresh_auto_selection.assert_called_once()
    frame._selector.refresh.assert_not_called()  # Success is delivered via the switch event.
    frame._contract_rollover_timer.StartOnce.assert_called_once_with(_MAX_TIMER_DELAY_MS)


def test_switch_signal_updates_ui_on_ui_thread_and_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = _frame(datetime(2026, 9, 16, 13, 29, tzinfo=TAIPEI_TZ))
    call_after = MagicMock()
    monkeypatch.setattr("tfx_quant.desktop.readiness_frame.wx.CallAfter", call_after)
    ReadinessFrame._on_contract_changed(frame, MagicMock())  # type: ignore[arg-type]
    frame._refresh.assert_not_called()
    call_after.assert_called_once_with(frame._apply_contract_change)
    call_after.call_args.args[0]()
    frame._refresh.assert_called_once()
    frame._contract_rollover_timer.StartOnce.assert_called_once_with(60_000)


def test_failed_rollover_displays_error_without_timer_loop() -> None:
    frame = _frame(datetime(2026, 9, 16, 13, 30, tzinfo=TAIPEI_TZ))
    selection = frame._services.instrument_selection
    selection.auto_refresh_error = "missing next contract"
    selection.refresh_auto_selection.return_value = False
    ReadinessFrame._on_contract_rollover_wake(frame, None)  # type: ignore[arg-type]
    frame._selector.refresh.assert_called_once()
    frame._contract_rollover_timer.StartOnce.assert_not_called()


def test_close_cancels_rollover_and_ignores_pending_ui_signal() -> None:
    frame = _frame(datetime(2026, 9, 16, 13, 30, tzinfo=TAIPEI_TZ))
    ReadinessFrame._on_close(frame, MagicMock())  # type: ignore[arg-type]
    frame._contract_rollover_timer.Stop.assert_called_once()
    ReadinessFrame._on_contract_rollover_wake(frame, None)  # type: ignore[arg-type]
    frame._apply_contract_change()
    frame._services.instrument_selection.refresh_auto_selection.assert_not_called()
    frame._refresh.assert_not_called()
