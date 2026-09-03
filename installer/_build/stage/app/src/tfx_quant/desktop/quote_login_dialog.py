"""QuoteLoginDialog — quote-only credentials for the 測試環境.

In the 測試環境 the trade adapter is the local simulator (no credentials, no server), so
the only login the operator enters is the real Yuanta *quote* feed: a user id, a
password, and an optional "safely store" checkbox. No environment radio, no certificate
import, no trading-account selection.
"""

from __future__ import annotations

import wx

from tfx_quant.infrastructure.yuanta.credentials import (
    load_stored_quote_password,
    store_quote_password,
)
from tfx_quant.telemetry import get_logger, log_info
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)


class QuoteLoginDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, *, remembered_user_id: str | None = None) -> None:
        super().__init__(parent, title="行情登入（測試環境）")
        self._user_id: str | None = None
        self._password: str | None = None

        grid = wx.FlexGridSizer(2, 2, 8, 8)
        grid.Add(wx.StaticText(self, label="行情帳號"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._user = wx.TextCtrl(self, value=remembered_user_id or "")
        grid.Add(self._user, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="行情密碼"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._password_ctrl = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        grid.Add(self._password_ctrl, 1, wx.EXPAND)
        grid.AddGrowableCol(1)

        self._remember = wx.CheckBox(self, label="安全儲存密碼（Windows 認證管理員）")
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(grid, 0, wx.EXPAND | wx.ALL, 12)
        outer.Add(self._remember, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(outer)

        if remembered_user_id:
            stored = load_stored_quote_password(remembered_user_id)
            if stored:
                self._password_ctrl.SetValue(stored)
                self._remember.SetValue(True)

    @property
    def credentials(self) -> tuple[str, str] | None:
        if self._user_id is None or self._password is None:
            return None
        return self._user_id, self._password

    def _on_ok(self, _event: wx.CommandEvent) -> None:
        user_id = self._user.GetValue().strip()
        password = self._password_ctrl.GetValue()
        if not user_id or not password:
            wx.MessageBox("行情帳號與密碼為必填", "行情登入", wx.OK | wx.ICON_WARNING, self)
            return
        if self._remember.IsChecked():
            store_quote_password(user_id, password)
        self._user_id, self._password = user_id, password
        log_info(_logger, "test_env_quote_login_submitted", user_id_masked=mask_account(user_id))
        self.EndModal(wx.ID_OK)


__all__ = ["QuoteLoginDialog"]
