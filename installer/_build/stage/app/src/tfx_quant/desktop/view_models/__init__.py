"""wx-free view-models for the trading-activity panel.

These modules import only `application` and `domain`, so the display logic (row shaping,
formatting, drill-down assembly) is unit-tested without a `wx.App`. The panel is thin
glue over them.
"""
