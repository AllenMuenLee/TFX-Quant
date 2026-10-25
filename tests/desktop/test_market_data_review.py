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


def test_market_data_panel_resets_view_and_sets_empty_message_on_contract_change() -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from tfx_quant.application.ports.quote_gateway import QuoteConnectionState

    canvas = MagicMock()
    status = MagicMock()
    review_warning = MagicMock()
    review_button = MagicMock()
    layout = MagicMock()

    current_sel = SimpleNamespace(
        instrument=Instrument.TXF,
        contract=ContractMonth(2026, 11),
    )
    selection_service = SimpleNamespace(current=current_sel)
    quote_runtime = SimpleNamespace(
        refresh=MagicMock(),
        query=MagicMock(return_value=[]),
        forming_bar=None,
        last_event_at=None,
        event_count=0,
        state=QuoteConnectionState.LOGGED_ON,
        has_market_data_warning=False,
    )
    clock = SimpleNamespace(now=MagicMock(return_value=Timestamp(datetime(2026, 10, 24, 10, 0, tzinfo=TAIPEI_TZ))))
    services = SimpleNamespace(
        quote_runtime=quote_runtime,
        instrument_selection=selection_service,
        clock=clock,
    )

    panel = SimpleNamespace(
        _services=services,
        _live_mode=True,
        _displayed_target=(Instrument.MXF, ContractMonth(2026, 10)),
        _canvas=canvas,
        _status=status,
        _review_warning=review_warning,
        _review_button=review_button,
        Layout=layout,
    )

    MarketDataPanel.refresh(panel)  # type: ignore[arg-type]

    # Verify target was updated and reset_view was triggered on canvas
    assert panel._displayed_target == (Instrument.TXF, ContractMonth(2026, 11))
    canvas.reset_view.assert_called_once()
    # Verify set_bars was called with empty list and informative empty_message
    canvas.set_bars.assert_called_once()
    bars_arg, kwargs = canvas.set_bars.call_args
    assert bars_arg[0] == []
    assert f"{Instrument.TXF.display_name_zh} 202611 尚無已收盤 K 棒，等待即時行情形成中…" in kwargs["empty_message"]


def test_candlestick_canvas_handles_empty_bars_without_error() -> None:
    import wx
    from tfx_quant.desktop.market_data_panel import CandlestickCanvas

    app = wx.GetApp() or wx.App()
    frame = wx.Frame(None)
    canvas = CandlestickCanvas(frame)
    canvas.SetSize(300, 200)

    # Empty bars
    canvas.set_bars([], "無資料")
    assert canvas._bars == []
    assert canvas._empty_message == "無資料"
    start, end = canvas._visible_range(200)
    assert (start, end) == (0, 0)

    frame.Destroy()
