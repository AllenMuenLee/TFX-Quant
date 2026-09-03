"""Yuanta login credentials.

Per `docs/secrets-management.md`: credentials never live in `TradingSettings` or any
committed/local JSON file. The 元大歸戶 ID and password are typed into the login screen
(`desktop/login_dialog.py`) each session; the only thing optionally persisted across
runs is the password, and only when the operator explicitly checks "安全儲存密碼" —
written to Windows Credential Manager via the `keyring` package (DPAPI-protected under
the hood), keyed by user ID, never anything hardcoded and never logged. The 元大歸戶 ID
itself may be separately remembered (non-secret) — see
`infrastructure/yuanta/login_preferences.py`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from tfx_quant.infrastructure.yuanta.errors import CertificateImportError
from tfx_quant.telemetry import get_logger, log_error, log_info
from tfx_quant.telemetry.masking import mask_account

_logger = get_logger(__name__)

KEYRING_SERVICE_NAME = "tfx_quant.yuanta"
QUOTE_KEYRING_SERVICE_NAME = "tfx_quant.yuanta.quote"
"""A *separate* keyring namespace for the quote-only credential used by the 測試環境,
so a stored quote password can never be picked up as a trade-login password (and the
reverse)."""
CERTIFICATE_KEYRING_USER = "certificate-import-password"


@dataclass(frozen=True, slots=True)
class BrokerCredentials:
    """In-memory login credentials, as handed to the trade/quote OCX adapters.
    `password` is a `SecretStr` so `repr()`/`str()`/logging never print the raw value
    (see docs/secrets-management.md)."""

    user_id: str
    password: SecretStr


def load_stored_password(user_id: str) -> str | None:
    """Reads a previously secure-stored password for `user_id` from Windows Credential
    Manager/DPAPI via `keyring`. Returns `None` if nothing is stored (including when
    the `keyring` package itself isn't importable — the caller treats that the same as
    "not stored" and falls back to requiring the password field)."""
    try:
        import keyring
    except ImportError:
        log_info(
            _logger,
            "stored_password_load_result",
            user_id_masked=mask_account(user_id),
            found=False,
        )
        return None
    password = keyring.get_password(KEYRING_SERVICE_NAME, user_id)
    log_info(
        _logger,
        "stored_password_load_result",
        user_id_masked=mask_account(user_id),
        found=password is not None,
    )
    return password


def store_password(user_id: str, password: str) -> None:
    """Secure-stores `password` for `user_id` — only called after a *successful*
    login, and only when the operator checked "安全儲存密碼" (see
    `desktop/login_dialog.py`), never unconditionally on every submit."""
    import keyring

    keyring.set_password(KEYRING_SERVICE_NAME, user_id, password)
    log_info(_logger, "stored_password_saved", user_id_masked=mask_account(user_id))


def clear_stored_password(user_id: str) -> None:
    """Removes any secure-stored password for `user_id`. Safe to call when nothing is
    stored (a no-op) — backs the login screen's "清除已儲存密碼" action."""
    import contextlib

    import keyring
    import keyring.errors

    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(KEYRING_SERVICE_NAME, user_id)
    log_info(_logger, "stored_password_cleared", user_id_masked=mask_account(user_id))


def load_stored_quote_password(user_id: str) -> str | None:
    """Quote-only counterpart of `load_stored_password`, keyed under
    `QUOTE_KEYRING_SERVICE_NAME`. Used by the 測試環境's quote-login dialog."""
    try:
        import keyring
    except ImportError:
        return None
    password = keyring.get_password(QUOTE_KEYRING_SERVICE_NAME, user_id)
    log_info(
        _logger,
        "stored_quote_password_load_result",
        user_id_masked=mask_account(user_id),
        found=password is not None,
    )
    return password


def store_quote_password(user_id: str, password: str) -> None:
    import keyring

    keyring.set_password(QUOTE_KEYRING_SERVICE_NAME, user_id, password)
    log_info(_logger, "stored_quote_password_saved", user_id_masked=mask_account(user_id))


def clear_stored_quote_password(user_id: str) -> None:
    import contextlib

    import keyring
    import keyring.errors

    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(QUOTE_KEYRING_SERVICE_NAME, user_id)
    log_info(_logger, "stored_quote_password_cleared", user_id_masked=mask_account(user_id))


def store_certificate_password(password: str) -> None:
    """Store the PFX password in the OS credential vault, never in preferences JSON."""
    import keyring

    keyring.set_password(KEYRING_SERVICE_NAME, CERTIFICATE_KEYRING_USER, password)


def clear_certificate_password() -> None:
    """Removes the stored PFX password. Safe to call when nothing is stored (a no-op).

    Called when the operator imports a *different* certificate without asking to
    remember its password: there is only one certificate keyring entry, so leaving the
    previous file's password behind would let it be silently reused for the new one."""
    import contextlib

    import keyring
    import keyring.errors

    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(KEYRING_SERVICE_NAME, CERTIFICATE_KEYRING_USER)
    log_info(_logger, "certificate_password_cleared")


def load_certificate_password() -> str | None:
    """Load the PFX password used by automatic startup certificate import."""
    try:
        import keyring
    except ImportError:
        return None
    return keyring.get_password(KEYRING_SERVICE_NAME, CERTIFICATE_KEYRING_USER)


def ensure_certificate_imported(certificate_path: str, certificate_password: SecretStr) -> None:
    """Imports a Yuanta login certificate (`.pfx`) into the current Windows user's
    personal certificate store, via the built-in `certutil` tool.

    This is an application-level certificate-store operation, not a broker API call —
    the official docs only say the certificate must be "匯入至電腦" (imported into the
    computer) before `Login(Account, Pass)` will work on Windows; they don't prescribe
    how (see 前言 > 測試環境&正式環境說明). `certutil -importpfx` is the standard
    built-in Windows mechanism for a per-user (no admin rights required) PFX import —
    matching this codebase's existing "no admin rights needed" posture (see
    `docs/adr/0004-broker-session-architecture.md`). This lets the login screen offer a
    one-click import instead of sending the operator to a manual Certificate Manager
    dialog outside the app.

    The certificate password is piped via stdin rather than passed as a `-p` CLI
    argument, so it never appears in the process argument list (visible to other
    processes/audit tools) — matching `docs/secrets-management.md`'s "never let a
    secret leak into a place other than where it's strictly needed" posture.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["certutil", "-importpfx", "-user", "-f", str(certificate_path)],
            input=f"{certificate_password.get_secret_value()}\n",
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        log_error(_logger, "certificate_import_failed", reason="certutil not found")
        raise CertificateImportError("找不到 certutil 工具，僅支援 Windows 環境") from exc
    if completed.returncode != 0:
        log_error(
            _logger,
            "certificate_import_failed",
            reason="certutil non-zero exit",
            certutil_exit_code=completed.returncode,
        )
        raise CertificateImportError(
            f"憑證匯入失敗（certutil 結束碼 {completed.returncode}）；"
            "請確認憑證檔案路徑與憑證密碼是否正確。"
        )
    log_info(_logger, "certificate_imported")


def certificate_path_exists(certificate_path: str) -> bool:
    """Cheap existence check the login UI can use before offering the import button —
    does not itself touch the Windows certificate store."""
    return Path(certificate_path).is_file()
