from __future__ import annotations

import pytest
from pydantic import SecretStr

from tfx_quant.infrastructure.yuanta.credentials import (
    KEYRING_SERVICE_NAME,
    BrokerCredentials,
    certificate_path_exists,
    clear_stored_password,
    ensure_certificate_imported,
    load_stored_password,
    store_password,
)
from tfx_quant.infrastructure.yuanta.errors import CertificateImportError


def test_password_repr_never_leaks_the_raw_value() -> None:
    credentials = BrokerCredentials(user_id="A123456789", password=SecretStr("hunter2"))

    assert "hunter2" not in repr(credentials)
    assert "hunter2" not in str(credentials)


def test_load_stored_password_returns_none_when_nothing_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("keyring.get_password", lambda service, user: None)

    assert load_stored_password("A123456789") is None


def test_load_stored_password_returns_stored_value(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_get_password(service: str, user: str) -> str:
        calls.append((service, user))
        return "hunter2"

    monkeypatch.setattr("keyring.get_password", fake_get_password)

    assert load_stored_password("A123456789") == "hunter2"
    assert calls == [(KEYRING_SERVICE_NAME, "A123456789")]


def test_store_password_writes_through_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, user, password: calls.append((service, user, password)),
    )

    store_password("A123456789", "hunter2")

    assert calls == [(KEYRING_SERVICE_NAME, "A123456789", "hunter2")]


def test_clear_stored_password_deletes_through_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        "keyring.delete_password", lambda service, user: calls.append((service, user))
    )

    clear_stored_password("A123456789")

    assert calls == [(KEYRING_SERVICE_NAME, "A123456789")]


def test_clear_stored_password_is_a_no_op_when_nothing_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring.errors

    def raise_not_found(service: str, user: str) -> None:
        raise keyring.errors.PasswordDeleteError("not found")

    monkeypatch.setattr("keyring.delete_password", raise_not_found)

    clear_stored_password("A123456789")  # must not raise


def test_certificate_path_exists_false_for_missing_file(tmp_path: object) -> None:
    assert certificate_path_exists(str(tmp_path) + "/does-not-exist.pfx") is False  # type: ignore[operator]


def test_certificate_path_exists_true_for_real_file(tmp_path: object) -> None:
    cert_path = tmp_path / "cert.pfx"  # type: ignore[operator]
    cert_path.write_bytes(b"not a real pfx")
    assert certificate_path_exists(str(cert_path)) is True


def test_ensure_certificate_imported_raises_on_certutil_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailedResult:
        returncode = 1

    monkeypatch.setattr(
        "tfx_quant.infrastructure.yuanta.credentials.subprocess.run",
        lambda *a, **k: _FailedResult(),
    )

    with pytest.raises(CertificateImportError):
        ensure_certificate_imported("C:\\fake.pfx", SecretStr("pw"))


def test_ensure_certificate_imported_succeeds_and_never_passes_password_as_an_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The certificate password must be piped via stdin, never a CLI argument — a CLI
    argument would be visible to other processes on the machine (see
    docs/secrets-management.md)."""
    captured: dict[str, object] = {}

    class _OkResult:
        returncode = 0

    def fake_run(args: list[str], **kwargs: object) -> _OkResult:
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        return _OkResult()

    monkeypatch.setattr("tfx_quant.infrastructure.yuanta.credentials.subprocess.run", fake_run)

    ensure_certificate_imported("C:\\fake.pfx", SecretStr("super-secret-password"))

    assert "super-secret-password" not in captured["args"]  # type: ignore[operator]
    assert "super-secret-password" in str(captured["input"])
