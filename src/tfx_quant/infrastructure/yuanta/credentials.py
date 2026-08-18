"""Yuanta login credential loading.

Per `docs/secrets-management.md`: credentials never live in `TradingSettings` or any
committed/local JSON file. The real source (`EnvironmentAndKeyringCredentialSource`)
reads the 元大歸戶 ID from an environment variable **by name** and the password from
Windows Credential Manager via the `keyring` package (DPAPI-protected under the hood)
— never anything hardcoded, never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from pydantic import SecretStr

from tfx_quant.infrastructure.yuanta.errors import CredentialsUnavailableError

USER_ID_ENV_VAR = "TFX_QUANT_YUANTA_USER_ID"
"""元大歸戶 ID — read by name from the environment, never stored in settings.json."""

ACCOUNT_NO_ENV_VAR = "TFX_QUANT_YUANTA_ACCOUNT_NO"
"""Optional: disambiguates which futures account to auto-select when login returns
more than one. Same OS-level-secret pattern as credentials — deliberately NOT a
`TradingSettings` field (see docs/secrets-management.md's rule against a raw account
number in settings). Unset means the UI must call `select_account()` explicitly."""

KEYRING_SERVICE_NAME = "tfx_quant.yuanta"


@dataclass(frozen=True, slots=True)
class BrokerCredentials:
    """In-memory login credentials. `password` is a `SecretStr` so `repr()`/`str()`/
    logging never print the raw value (see docs/secrets-management.md)."""

    user_id: str
    password: SecretStr


class CredentialSource(Protocol):
    def load(self) -> BrokerCredentials: ...


class EnvironmentAndKeyringCredentialSource:
    """Implements `CredentialSource` — the real, production credential source."""

    def load(self) -> BrokerCredentials:
        user_id = os.environ.get(USER_ID_ENV_VAR)
        if not user_id or not user_id.strip():
            raise CredentialsUnavailableError(
                f"未設定環境變數 {USER_ID_ENV_VAR}（元大歸戶 ID）；"
                "請於系統環境變數中設定後重新啟動程式。"
            )

        try:
            import keyring
        except ImportError as exc:
            raise CredentialsUnavailableError(
                "缺少 keyring 套件，無法從 Windows 認證管理員讀取密碼；"
                "請執行 `pip install keyring` 後重新啟動程式。"
            ) from exc

        password = keyring.get_password(KEYRING_SERVICE_NAME, user_id)
        if not password:
            raise CredentialsUnavailableError(
                f"Windows 認證管理員中找不到服務 {KEYRING_SERVICE_NAME!r} "
                "對應此使用者的密碼；請使用「認證管理員」新增一筆通用認證"
                f"（網際網路或網路位址填 {KEYRING_SERVICE_NAME}，使用者名稱填元大歸戶 "
                "ID），或執行 `keyring set` 指令設定後重新啟動程式。"
            )

        return BrokerCredentials(user_id=user_id, password=SecretStr(password))


class StaticCredentialSource:
    """Implements `CredentialSource` — fixed in-memory credentials, for tests only."""

    def __init__(self, credentials: BrokerCredentials) -> None:
        self._credentials = credentials

    def load(self) -> BrokerCredentials:
        return self._credentials
