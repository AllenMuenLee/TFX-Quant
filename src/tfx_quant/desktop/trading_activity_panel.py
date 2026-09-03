"""委託／成交・持倉・損益・交易報告 — one panel, four tabs, shared by 正式環境 and 測試環境.

Every tab is thin glue over a wx-free view-model (`desktop.view_models`); the panel only
maps rows to `wx.ListCtrl` lines and refreshes on a timer plus the relevant events. In
UAT every row is simulated, so the P&L / positions / report tabs also carry the
`simulation=true` marking their view-models already set.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import wx

from tfx_quant.application.events.events import (
    LatestPriceObserved,
    OrderStateTransitioned,
    TradeLedgerFillRecorded,
)
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.desktop.view_models.orders_view_model import build_orders_view
from tfx_quant.desktop.view_models.pnl_view_model import build_pnl_view
from tfx_quant.desktop.view_models.positions_view_model import build_positions_view
from tfx_quant.desktop.view_models.trade_report_view_model import build_trade_report_view

_ATTENTION_BG = wx.Colour(120, 30, 30)
_SIM_BG = wx.Colour(60, 45, 12)


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


class TradingActivityPanel(wx.Panel):
    def __init__(self, parent: wx.Window, services: ServiceContainer) -> None:
        super().__init__(parent)
        self._services = services
        notebook = wx.Notebook(self)
        self._orders = _grid(
            notebook, ["本機單號", "券商單號", "方向", "開平", "口數", "已成交", "狀態", "時間"]
        )
        self._positions = _grid(
            notebook, ["商品", "契約", "淨口數", "均價", "現價", "未實現", "資料品質", "更新時間"]
        )
        self._pnl = _grid(
            notebook, ["期間", "毛損益", "手續費", "交易稅", "淨損益", "口數", "暫定", "模擬"]
        )
        self._report = _grid(
            notebook,
            ["交易日", "商品", "方向", "口數", "開倉價", "平倉價", "毛損益", "淨損益", "模擬"],
        )
        notebook.AddPage(self._orders, "委託／成交")
        notebook.AddPage(self._positions, "持倉")
        notebook.AddPage(self._pnl, "損益")
        notebook.AddPage(self._report, "交易報告")

        self._summary = wx.StaticText(self, label="")
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self._summary, 0, wx.ALL, 6)
        outer.Add(notebook, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda _e: self.refresh(), self._timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._unsubscribers = [
            services.event_coordinator.subscribe(OrderStateTransitioned, self._on_event),
            services.event_coordinator.subscribe(TradeLedgerFillRecorded, self._on_event),
            services.event_coordinator.subscribe(LatestPriceObserved, self._on_event),
        ]
        self._timer.Start(2000)
        self.refresh()

    def _on_event(self, _event: object) -> None:
        wx.CallAfter(self.refresh)

    def _on_destroy(self, event: wx.WindowDestroyEvent) -> None:
        if event.GetEventObject() is self:
            self._timer.Stop()
            for unsubscribe in self._unsubscribers:
                unsubscribe()
        event.Skip()

    def refresh(self) -> None:
        services = self._services
        end = date.today()
        start = end - timedelta(days=62)

        orders = build_orders_view(services.order_repository)
        _fill_grid(
            self._orders,
            [
                (
                    [
                        r.local_order_id[:8],
                        r.broker_order_no or "—",
                        r.direction,
                        r.effect,
                        str(r.lots),
                        str(r.cumulative_filled),
                        r.status,
                        r.updated_at,
                    ],
                    r.needs_attention,
                    False,
                )
                for r in orders
            ],
        )

        positions = build_positions_view(services.position_valuation_service)
        _fill_grid(
            self._positions,
            [
                (
                    [
                        p.instrument,
                        p.contract,
                        str(p.net_lots),
                        f"{p.avg_cost:,.2f}",
                        "—" if p.mark_price is None else f"{p.mark_price:,.2f}",
                        _money(p.unrealized_pnl),
                        p.price_quality,
                        p.last_price_at or "—",
                    ],
                    p.unrealized_pnl is None,
                    positions.simulation,
                )
                for p in positions.rows
            ],
        )

        pnl = build_pnl_view(services.trade_report_facade, start, end)
        _fill_grid(
            self._pnl,
            [
                (
                    [
                        r.period,
                        _money(r.gross_pnl),
                        _money(r.commission),
                        _money(r.tax),
                        _money(r.net_pnl),
                        str(r.filled_lots),
                        "是" if r.provisional else "",
                        "是" if r.simulation else "",
                    ],
                    r.provisional,
                    r.simulation,
                )
                for r in pnl.daily
            ],
        )

        report = build_trade_report_view(services.trade_report_facade, start, end).report
        _fill_grid(
            self._report,
            [
                (
                    [
                        t.trading_day.isoformat(),
                        t.instrument.value,
                        t.side.value,
                        str(t.quantity),
                        f"{t.open_price:,.2f}",
                        f"{t.close_price:,.2f}",
                        _money(t.gross_pnl),
                        _money(t.net_pnl),
                        "是" if t.simulation else "",
                    ],
                    t.provisional,
                    t.simulation,
                )
                for t in report.realized_trades
            ],
        )

        marker = "　[模擬]" if positions.simulation else ""
        total = "—" if positions.total_pnl is None else f"{positions.total_pnl:,.0f}"
        self._summary.SetLabel(
            f"已實現 {positions.realized_pnl:,.0f}　未實現 {_money(positions.unrealized_pnl)}　"
            f"總計 {total}{marker}"
        )


def _grid(parent: wx.Window, columns: list[str]) -> wx.ListCtrl:
    grid = wx.ListCtrl(parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
    for index, name in enumerate(columns):
        grid.InsertColumn(index, name, width=wx.LIST_AUTOSIZE_USEHEADER)
    return grid


def _fill_grid(grid: wx.ListCtrl, rows: list[tuple[list[str], bool, bool]]) -> None:
    grid.DeleteAllItems()
    for cells, attention, simulated in rows:
        row = grid.InsertItem(grid.GetItemCount(), cells[0])
        for column, value in enumerate(cells[1:], start=1):
            grid.SetItem(row, column, value)
        if attention:
            grid.SetItemBackgroundColour(row, _ATTENTION_BG)
        elif simulated:
            grid.SetItemBackgroundColour(row, _SIM_BG)


__all__ = ["TradingActivityPanel"]
