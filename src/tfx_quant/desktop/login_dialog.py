"""Login dialog for Yuanta's separate order and quote OCX sessions.

Implements `implementation prompt/02-yuanta-api-session/login-input-implementation-
prompt.md`: collects 執行環境, 帳號, 密碼, 記住帳號, and 安全儲存密碼 from the operator,
builds a `LoginRequest` (`application/ports/broker_session.py`), and hands it to
`services.broker_session.start()`. Never touches the `pythonnet`/CLR layer directly —
that stays inside the adapter `IBrokerSession` wraps.

The validation/DTO-building logic (`build_login_request`) is deliberately a plain,
wx-free function so it is testable without a live `wx.App`. The selected environment
routes only the order connection; the quote API has one documented host and a separate
login call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import wx
from pydantic import SecretStr

from tfx_quant.application.events.events import (
    BrokerLoginFailed,
    BrokerLoginSucceeded,
    BrokerLoginTimedOut,
)
from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.desktop.composition import ServiceContainer
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.infrastructure.yuanta import credentials, login_preferences
from tfx_quant.infrastructure.yuanta.errors import CertificateImportError
from tfx_quant.telemetry import get_logger, log_info, log_warning
from tfx_quant.telemetry.masking import field_present, mask_account

_logger = get_logger(__name__)

_ENVIRONMENT_CHOICES: tuple[Environment, ...] = (Environment.TEST, Environment.PRODUCTION)
_ENVIRONMENT_LABELS = (
    "交易測試 UAT（行情 API 仍使用唯一正式主機）",
    "交易正式 PROD (PRODUCTION)",
)

_PRODUCTION_CONFIRM_MESSAGE = (
    "即將以「正式環境」登入元大期貨 API，登入成功後可能影響真實帳戶／交易相關資料。\n"
    "請確認您確實要使用正式環境登入。"
)


class LoginFormError(Exception):
    """A field failed validation — the dialog shows `str(exc)` to the operator."""


class CertificateFormError(Exception):
    """A certificate-import field failed validation — the dialog shows `str(exc)` to
    the operator."""


def build_login_request(
    *,
    environment: Environment,
    user_id: str,
    password: str,
    stored_password: str | None,
) -> LoginRequest:
    """Pure validation + DTO construction — no wx, no I/O.

    `user_id` has its surrounding whitespace stripped (the implementation prompt's
    "去除 ID 首尾空白但不得改寫密碼"); `password` is never stripped/altered. If
    `password` is left blank and a previously secure-stored password exists for this
    ID (`stored_password`, fetched by the caller via `credentials.load_stored_password`
    regardless of the current "安全儲存密碼" checkbox state), that stored value is used
    — the checkbox only controls whether *this* submission's password gets (re)saved
    afterward, not whether a blank field can fall back to what's already stored.
    """
    user_id = user_id.strip()
    if not user_id:
        raise LoginFormError("帳號不可空白")

    if not password:
        password = stored_password or ""
    if not password:
        raise LoginFormError("密碼不可空白")

    return LoginRequest(
        environment=environment,
        user_id=user_id,
        password=SecretStr(password),
    )


def _same_certificate_file(left: str, right: str | None) -> bool:
    """Whether two path strings name the same file, for deciding if a remembered
    password still belongs to the certificate being imported."""
    if not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def build_certificate_import_request(
    *,
    certificate_path: str,
    certificate_password: str,
    stored_certificate_password: str | None,
    stored_certificate_path: str | None = None,
) -> tuple[str, SecretStr]:
    """Pure validation for the certificate-import controls — mirrors
    `build_login_request`'s wx-free split so it is testable without a live `wx.App`.

    Per the vendor docs (前言 > 測試環境＆正式環境說明), the login certificate must be
    imported into the Windows certificate store before `SetFutOrdConnection` will
    succeed on the production endpoint (`OnLogonS` TLinkStatus `4`/`lsCAError`
    otherwise). A blank password falls back to a previously secure-stored one, the
    same "記住密碼" semantics as `build_login_request` — but **only for the certificate
    that password was stored for**. There is one global keyring entry
    (`credentials.CERTIFICATE_KEYRING_USER`), not one per file, so reusing it for a
    newly chosen `.pfx` just feeds `certutil` the wrong password: the import fails with
    an opaque exit code and the operator is never told the blank box silently supplied
    a stale secret.
    """
    certificate_path = certificate_path.strip()
    if not certificate_path:
        raise CertificateFormError("憑證檔案路徑不可空白")
    if not credentials.certificate_path_exists(certificate_path):
        raise CertificateFormError("找不到指定的憑證檔案")

    if not certificate_password and _same_certificate_file(
        certificate_path, stored_certificate_path
    ):
        certificate_password = stored_certificate_password or ""
    if not certificate_password:
        raise CertificateFormError("憑證密碼不可空白")

    return certificate_path, SecretStr(certificate_password)


@dataclass(frozen=True, slots=True)
class _FormValues:
    environment: Environment
    user_id: str
    password: str
    remember_id: bool
    secure_store: bool


class LoginDialog(wx.Dialog):
    """Modal login form. `ShowModal()` returns `wx.ID_OK` once `BrokerLoginSucceeded`
    is observed, or `wx.ID_CANCEL` if the operator cancels."""

    def __init__(self, parent: wx.Window | None, services: ServiceContainer) -> None:
        super().__init__(parent, title="元大交易／行情 API 登入", style=wx.DEFAULT_DIALOG_STYLE)
        self._services = services
        self._connecting = False
        self._pending_secure_store = False
        self._pending_quote_credentials: tuple[str, SecretStr] | None = None
        self._password_masked = True

        prefs = login_preferences.load()

        panel = wx.Panel(self)
        self._panel = panel
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._environment_radio = wx.RadioBox(
            panel, label="執行環境", choices=list(_ENVIRONMENT_LABELS)
        )
        self._environment_radio.SetSelection(0)  # 預設測試
        sizer.Add(self._environment_radio, 0, wx.ALL | wx.EXPAND, 8)

        id_row = wx.BoxSizer(wx.HORIZONTAL)
        id_row.Add(wx.StaticText(panel, label="帳號："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
        self._user_id_ctrl = wx.TextCtrl(panel, value=prefs.remembered_user_id or "")
        id_row.Add(self._user_id_ctrl, 1, wx.ALL | wx.EXPAND, 6)
        sizer.Add(id_row, 0, wx.EXPAND)
        self._password_row_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._password_row_sizer.Add(
            wx.StaticText(panel, label="密碼："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._password_ctrl: wx.TextCtrl = self._make_password_ctrl(masked=True)
        self._password_row_sizer.Add(self._password_ctrl, 1, wx.ALL | wx.EXPAND, 6)
        self._toggle_password_button = wx.Button(panel, label="顯示", style=wx.BU_EXACTFIT)
        self._toggle_password_button.Bind(wx.EVT_BUTTON, self._on_toggle_password_visibility)
        self._password_row_sizer.Add(self._toggle_password_button, 0, wx.ALL, 6)
        sizer.Add(self._password_row_sizer, 0, wx.EXPAND)

        self._remember_id_checkbox = wx.CheckBox(panel, label="記住帳號")
        self._remember_id_checkbox.SetValue(prefs.remembered_user_id is not None)
        sizer.Add(self._remember_id_checkbox, 0, wx.ALL, 6)

        self._secure_store_checkbox = wx.CheckBox(panel, label="安全儲存密碼（Windows 認證管理員）")
        sizer.Add(self._secure_store_checkbox, 0, wx.ALL, 6)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 6)

        sizer.Add(
            wx.StaticText(panel, label="憑證匯入（正式環境登入前需完成一次）"), 0, wx.ALL, 6
        )
        cert_path_row = wx.BoxSizer(wx.HORIZONTAL)
        self._certificate_path_ctrl = wx.TextCtrl(panel, value=prefs.certificate_path or "")
        cert_path_row.Add(self._certificate_path_ctrl, 1, wx.ALL | wx.EXPAND, 6)
        self._browse_certificate_button = wx.Button(panel, label="瀏覽…", style=wx.BU_EXACTFIT)
        self._browse_certificate_button.Bind(wx.EVT_BUTTON, self._on_browse_certificate)
        cert_path_row.Add(self._browse_certificate_button, 0, wx.ALL, 6)
        sizer.Add(cert_path_row, 0, wx.EXPAND)

        cert_password_row = wx.BoxSizer(wx.HORIZONTAL)
        cert_password_row.Add(
            wx.StaticText(panel, label="憑證密碼："), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6
        )
        self._certificate_password_ctrl = wx.TextCtrl(panel, style=wx.TE_PASSWORD)
        cert_password_row.Add(self._certificate_password_ctrl, 1, wx.ALL | wx.EXPAND, 6)
        sizer.Add(cert_password_row, 0, wx.EXPAND)

        self._remember_certificate_password_checkbox = wx.CheckBox(panel, label="記住憑證密碼")
        sizer.Add(self._remember_certificate_password_checkbox, 0, wx.ALL, 6)

        cert_button_row = wx.BoxSizer(wx.HORIZONTAL)
        cert_button_row.AddStretchSpacer()
        self._import_certificate_button = wx.Button(panel, label="匯入憑證")
        self._import_certificate_button.Bind(wx.EVT_BUTTON, self._on_import_certificate)
        cert_button_row.Add(self._import_certificate_button, 0, wx.ALL, 6)
        sizer.Add(cert_button_row, 0, wx.EXPAND)

        self._certificate_status_label = wx.StaticText(panel, label="")
        self._certificate_status_label.Wrap(360)
        sizer.Add(self._certificate_status_label, 0, wx.ALL | wx.EXPAND, 8)

        sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 6)

        self._status_label = wx.StaticText(panel, label="")
        self._status_label.Wrap(360)
        sizer.Add(self._status_label, 0, wx.ALL | wx.EXPAND, 8)

        button_row = wx.BoxSizer(wx.HORIZONTAL)
        self._clear_stored_button = wx.Button(panel, label="清除已儲存密碼")
        self._clear_stored_button.Bind(wx.EVT_BUTTON, self._on_clear_stored_password)
        button_row.Add(self._clear_stored_button, 0, wx.ALL, 6)
        button_row.AddStretchSpacer()
        self._cancel_button = wx.Button(panel, id=wx.ID_CANCEL, label="取消")
        self._cancel_button.Bind(wx.EVT_BUTTON, self._on_cancel)
        button_row.Add(self._cancel_button, 0, wx.ALL, 6)
        self._submit_button = wx.Button(panel, label="登入")
        self._submit_button.Bind(wx.EVT_BUTTON, self._on_submit)
        button_row.Add(self._submit_button, 0, wx.ALL, 6)
        sizer.Add(button_row, 0, wx.EXPAND)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)

        self._unsubscribers = [
            services.event_coordinator.subscribe(BrokerLoginSucceeded, self._on_login_succeeded),
            services.event_coordinator.subscribe(BrokerLoginFailed, self._on_login_failed),
            services.event_coordinator.subscribe(BrokerLoginTimedOut, self._on_login_timed_out),
        ]
        self.Bind(wx.EVT_CLOSE, self._on_close)

        log_info(
            _logger,
            "login_form_opened",
            remembered_user_id_present=field_present(prefs.remembered_user_id),
        )

    # -- Password visibility toggle -------------------------------------------------

    def _make_password_ctrl(self, *, masked: bool) -> wx.TextCtrl:
        style = wx.TE_PASSWORD if masked else 0
        return wx.TextCtrl(self._panel, style=style)

    def _on_toggle_password_visibility(self, _event: wx.CommandEvent) -> None:
        # `wx.TE_PASSWORD` cannot be toggled on a live control on every platform —
        # the portable approach is to recreate the control with the new style,
        # carrying the current value across.
        value = self._password_ctrl.GetValue()
        self._password_masked = not self._password_masked
        old_ctrl = self._password_ctrl
        new_ctrl = self._make_password_ctrl(masked=self._password_masked)
        new_ctrl.SetValue(value)
        new_ctrl.Enable(old_ctrl.IsEnabled())
        self._password_row_sizer.Replace(old_ctrl, new_ctrl)
        old_ctrl.Destroy()
        self._password_ctrl = new_ctrl
        self._toggle_password_button.SetLabel("隱藏" if not self._password_masked else "顯示")
        self._panel.Layout()

    # -- Form state -------------------------------------------------------------

    def _read_form(self) -> _FormValues:
        return _FormValues(
            environment=_ENVIRONMENT_CHOICES[self._environment_radio.GetSelection()],
            user_id=self._user_id_ctrl.GetValue(),
            password=self._password_ctrl.GetValue(),
            remember_id=self._remember_id_checkbox.GetValue(),
            secure_store=self._secure_store_checkbox.GetValue(),
        )

    def _set_form_enabled(self, enabled: bool) -> None:
        for ctrl in (
            self._environment_radio,
            self._user_id_ctrl,
            self._password_ctrl,
            self._toggle_password_button,
            self._remember_id_checkbox,
            self._secure_store_checkbox,
            self._clear_stored_button,
            self._submit_button,
        ):
            ctrl.Enable(enabled)

    # -- Submit -------------------------------------------------------------------

    def _on_submit(self, _event: wx.CommandEvent) -> None:
        if self._connecting:
            log_info(_logger, "login_submit_blocked", reason="duplicate submit while connecting")
            return  # 禁止重複送出

        values = self._read_form()
        log_info(
            _logger,
            "login_environment_selected",
            environment=values.environment.value,
        )
        stripped_id = values.user_id.strip()
        stored_password = credentials.load_stored_password(stripped_id) if stripped_id else None
        try:
            request = build_login_request(
                environment=values.environment,
                user_id=values.user_id,
                password=values.password,
                stored_password=stored_password,
            )
        except LoginFormError as exc:
            log_info(
                _logger,
                "login_field_validation_failed",
                failure_phase=str(exc),
                user_id_provided=field_present(values.user_id),
                password_provided=field_present(values.password) or stored_password is not None,
            )
            self._status_label.SetLabel(str(exc))
            return
        log_info(
            _logger,
            "login_field_validation_succeeded",
            user_id_masked=mask_account(request.user_id),
        )

        if values.environment is Environment.PRODUCTION:
            confirmed_at = Timestamp.now()
            confirmed = (
                wx.MessageBox(
                    _PRODUCTION_CONFIRM_MESSAGE, "正式環境確認", wx.YES_NO | wx.ICON_WARNING, self
                )
                == wx.YES
            )
            log_info(
                _logger,
                "production_environment_confirmation",
                confirmed_at=confirmed_at.value.isoformat(),
                confirmed=confirmed,
            )
            if not confirmed:
                return

        login_preferences.save_remembered_user_id(request.user_id if values.remember_id else None)

        self._pending_secure_store = values.secure_store
        self._pending_quote_credentials = (request.user_id, request.password)
        self._connecting = True
        self._set_form_enabled(False)
        self._status_label.SetLabel("登入中…")
        log_info(_logger, "login_submitted", user_id_masked=mask_account(request.user_id))
        self._services.broker_session.start(request)

    # -- Broker events (arrive on EventCoordinator's own thread) --------------------

    def _on_login_succeeded(self, event: BrokerLoginSucceeded) -> None:
        wx.CallAfter(self._handle_login_succeeded, event)

    def _handle_login_succeeded(self, event: BrokerLoginSucceeded) -> None:
        if not self._connecting:
            return
        log_info(_logger, "login_result", succeeded=True, account_list_count=len(event.accounts))
        if self._pending_secure_store:
            values = self._read_form()
            user_id = values.user_id.strip()
            if user_id and values.password:
                credentials.store_password(user_id, values.password)
        if self._pending_quote_credentials is not None:
            user_id, password = self._pending_quote_credentials
            try:
                self._services.quote_runtime.start(user_id, password)
            except Exception as exc:
                log_warning(_logger, "quote_login_failed", reason=str(exc))
            finally:
                self._pending_quote_credentials = None
        self._connecting = False
        if self.IsModal():
            self.EndModal(wx.ID_OK)

    def _on_login_failed(self, event: BrokerLoginFailed) -> None:
        # `event.reason` is the only place this text previously reached — the wx
        # StaticText it's shown in wraps at a fixed width and can visually truncate a
        # long vendor message. Logging it here means the full text always lands in the
        # terminal (telemetry's StreamHandler) and tfx_quant.log too, not just the UI.
        log_warning(
            _logger, "login_result", succeeded=False, retriable=event.retriable, reason=event.reason
        )
        wx.CallAfter(self._handle_terminal_failure, f"登入失敗：{event.reason}")

    def _on_login_timed_out(self, _event: BrokerLoginTimedOut) -> None:
        if not self._connecting:
            return
        wx.CallAfter(self._status_label.SetLabel, "登入逾時，處理中…")

    def _handle_terminal_failure(self, message: str) -> None:
        if not self._connecting:
            return
        self._connecting = False
        self._set_form_enabled(True)
        self._status_label.SetLabel(message)

    # -- Certificate import ---------------------------------------------------------

    def _on_browse_certificate(self, _event: wx.CommandEvent) -> None:
        with wx.FileDialog(
            self,
            "選擇憑證檔案",
            wildcard="PFX 憑證檔 (*.pfx)|*.pfx|所有檔案 (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._certificate_path_ctrl.SetValue(dialog.GetPath())

    def _on_import_certificate(self, _event: wx.CommandEvent) -> None:
        path = self._certificate_path_ctrl.GetValue()
        password = self._certificate_password_ctrl.GetValue()
        stored_password = credentials.load_certificate_password()
        remembered = login_preferences.load()
        log_info(
            _logger,
            "certificate_import_requested",
            certificate_path_provided=field_present(path),
            certificate_password_provided=field_present(password) or stored_password is not None,
        )
        try:
            certificate_path, certificate_password = build_certificate_import_request(
                certificate_path=path,
                certificate_password=password,
                stored_certificate_password=stored_password,
                stored_certificate_path=remembered.certificate_path,
            )
        except CertificateFormError as exc:
            log_info(_logger, "certificate_import_validation_failed", failure_phase=str(exc))
            self._certificate_status_label.SetLabel(str(exc))
            return

        # Remembered before the attempt, not after it: the field prefills this dialog's
        # path box, so persisting only on success left the box silently reverting to a
        # previously imported certificate every time a newly chosen one failed — the
        # operator sees the old path and no explanation.
        if not _same_certificate_file(certificate_path, remembered.certificate_path):
            login_preferences.save_certificate_path(certificate_path)
            login_preferences.save_certificate_imported(False)

        try:
            credentials.ensure_certificate_imported(certificate_path, certificate_password)
        except CertificateImportError as exc:
            log_warning(_logger, "certificate_import_failed", reason=str(exc))
            self._certificate_status_label.SetLabel(str(exc))
            return

        login_preferences.save_certificate_path(certificate_path)
        login_preferences.save_certificate_imported(True)
        if self._remember_certificate_password_checkbox.GetValue():
            credentials.store_certificate_password(certificate_password.get_secret_value())
        elif not _same_certificate_file(certificate_path, remembered.certificate_path):
            # A different certificate is now the remembered one; the single keyring
            # entry still holds the previous file's password, which would otherwise be
            # offered for this one on the next blank-password import.
            credentials.clear_certificate_password()
        log_info(_logger, "certificate_import_succeeded")
        self._certificate_status_label.SetLabel("憑證匯入成功")

    # -- Clear stored password ----------------------------------------------------

    def _on_clear_stored_password(self, _event: wx.CommandEvent) -> None:
        user_id = self._user_id_ctrl.GetValue().strip()
        if not user_id:
            log_info(_logger, "clear_stored_password_requested", user_id_provided=False)
            self._status_label.SetLabel("請先輸入帳號再清除已儲存密碼")
            return
        log_info(
            _logger,
            "clear_stored_password_requested",
            user_id_provided=True,
            user_id_masked=mask_account(user_id),
        )
        credentials.clear_stored_password(user_id)
        self._status_label.SetLabel(f"已清除 {user_id} 的已儲存密碼")

    # -- Cancel / close -------------------------------------------------------------

    def _on_cancel(self, _event: wx.CommandEvent) -> None:
        self._close(wx.ID_CANCEL)

    def _on_close(self, _event: wx.CloseEvent) -> None:
        self._close(wx.ID_CANCEL)

    def _close(self, return_code: int) -> None:
        if self._connecting:
            self._services.broker_session.cancel_start()
            self._connecting = False
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        if self.IsModal():
            self.EndModal(return_code)
        else:
            self.Destroy()
