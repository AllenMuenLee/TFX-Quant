from __future__ import annotations

import pytest
from pydantic import SecretStr

from tfx_quant.infrastructure.yuanta.credentials import (
    USER_ID_ENV_VAR,
    BrokerCredentials,
    EnvironmentAndKeyringCredentialSource,
    StaticCredentialSource,
)
from tfx_quant.infrastructure.yuanta.errors import CredentialsUnavailableError


def test_missing_user_id_env_var_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(USER_ID_ENV_VAR, raising=False)
    source = EnvironmentAndKeyringCredentialSource()

    with pytest.raises(CredentialsUnavailableError, match=USER_ID_ENV_VAR):
        source.load()


def test_missing_keyring_entry_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_ID_ENV_VAR, "A123456789")
    monkeypatch.setattr("keyring.get_password", lambda service, user: None)
    source = EnvironmentAndKeyringCredentialSource()

    with pytest.raises(CredentialsUnavailableError):
        source.load()


def test_error_message_never_includes_the_user_id_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_ID_ENV_VAR, "SECRET_USER_ID_VALUE")
    monkeypatch.setattr("keyring.get_password", lambda service, user: None)
    source = EnvironmentAndKeyringCredentialSource()

    with pytest.raises(CredentialsUnavailableError) as exc_info:
        source.load()

    assert "SECRET_USER_ID_VALUE" not in str(exc_info.value)


def test_successful_load_returns_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(USER_ID_ENV_VAR, "A123456789")
    monkeypatch.setattr("keyring.get_password", lambda service, user: "hunter2")
    source = EnvironmentAndKeyringCredentialSource()

    credentials = source.load()

    assert credentials.user_id == "A123456789"
    assert credentials.password.get_secret_value() == "hunter2"


def test_password_repr_never_leaks_the_raw_value() -> None:
    credentials = BrokerCredentials(user_id="A123456789", password=SecretStr("hunter2"))

    assert "hunter2" not in repr(credentials)
    assert "hunter2" not in str(credentials)


def test_static_credential_source_returns_fixed_credentials() -> None:
    expected = BrokerCredentials(user_id="A123456789", password=SecretStr("hunter2"))
    source = StaticCredentialSource(expected)

    assert source.load() is expected
