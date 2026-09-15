from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import wx

from tfx_quant.desktop.market_data_panel import MarketDataPanel
from tfx_quant.desktop.readiness_frame import ReadinessFrame
from tfx_quant.domain.bar import Bar
from tfx_quant.domain.bar_record import BarDataSource, BarPeriod, BarRecord, MarketSession
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.money import Price
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


@pytest.mark.parametrize("answer", [wx.ID_YES, wx.ID_NO])
def test_review_dialog_only_confirms_the_displayed_record_after_explicit_yes(answer: int) -> None:
    start = Timestamp(datetime(2026, 9, 1, 15, 0, tzinfo=TAIPEI_TZ))
    end = Timestamp(datetime(2026, 9, 1, 16, 0, tzinfo=TAIPEI_TZ))
    price = Price(Decimal("18000"))
    record = BarRecord(
        Bar(Instrument.MXF, ContractMonth(2026, 9), price, price, price, price, 10, start, end),
        BarPeriod.SIXTY_MINUTE,
        start.value.date(),
        MarketSession.NIGHT,
        BarDataSource.LOCAL_YUANTA_REALTIME,
        False,
        end,
        end,
        is_complete=False,
    )
    runtime = MagicMock()
    runtime.query.return_value = [record]
    clock = MagicMock()
    clock.now.return_value = end
    panel = SimpleNamespace(
        _services=SimpleNamespace(quote_runtime=runtime, clock=clock),
        refresh=MagicMock(),
    )
    with (
        patch("tfx_quant.desktop.market_data_panel.wx.SingleChoiceDialog") as choice,
        patch("tfx_quant.desktop.market_data_panel.wx.MessageDialog") as confirmation,
    ):
        choice.return_value.__enter__.return_value.ShowModal.return_value = wx.ID_OK
        choice.return_value.__enter__.return_value.GetSelection.return_value = 0
        confirmation.return_value.__enter__.return_value.ShowModal.return_value = answer
        MarketDataPanel._on_review(panel, None)  # type: ignore[arg-type]
    if answer == wx.ID_YES:
        runtime.confirm_history_hour.assert_called_once_with(record)
    else:
        runtime.confirm_history_hour.assert_not_called()


def test_night_wake_connects_immediately_without_waiting_until_1500() -> None:
    frame = SimpleNamespace(_resume_market_data=MagicMock(), _arm_night_resume=MagicMock())
    with patch("tfx_quant.desktop.readiness_frame.wx.CallLater") as deferred:
        ReadinessFrame._on_night_resume_wake(frame, None)  # type: ignore[arg-type]
    frame._resume_market_data.assert_called_once()
    frame._arm_night_resume.assert_called_once()
    deferred.assert_not_called()
