"""Tests for `desktop/login_dialog.py`'s pure, wx-free `build_login_request` — the
validation/DTO-building logic split out specifically so it's testable without a live
`wx.App`, the same way `BrokerSessionOrchestrator` is split from its pythonnet/wx glue.
`LoginDialog` itself (the `wx.Dialog` shell) has no test here, matching this codebase's
existing convention of not unit-testing wx widgets directly.
"""

from __future__ import annotations

import pytest

from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.desktop.login_dialog import (
    CertificateFormError,
    LoginFormError,
    build_certificate_import_request,
    build_login_request,
)


def _build(**overrides: object):
    defaults: dict[str, object] = {
        "environment": Environment.TEST,
        "user_id": "F00000000012345678",
        "password": "hunter2",
        "stored_password": None,
    }
    defaults.update(overrides)
    return build_login_request(**defaults)  # type: ignore[arg-type]


def test_blank_user_id_raises() -> None:
    with pytest.raises(LoginFormError):
        _build(user_id="   ")


def test_user_id_whitespace_is_stripped() -> None:
    request = _build(user_id="  A123456789  ")

    assert request.user_id == "A123456789"


def test_password_whitespace_is_not_stripped() -> None:
    request = _build(password=" hunter2 ")

    assert request.password.get_secret_value() == " hunter2 "


def test_blank_password_with_no_stored_password_raises() -> None:
    with pytest.raises(LoginFormError):
        _build(password="", stored_password=None)


def test_blank_password_falls_back_to_stored_password() -> None:
    request = _build(password="", stored_password="stored-secret")

    assert request.password.get_secret_value() == "stored-secret"


def test_typed_password_takes_priority_over_stored_password() -> None:
    request = _build(password="typed-secret", stored_password="stored-secret")

    assert request.password.get_secret_value() == "typed-secret"


def test_environment_passes_through() -> None:
    request = _build(environment=Environment.PRODUCTION)

    assert request.environment is Environment.PRODUCTION


def test_password_repr_never_leaks_the_raw_value() -> None:
    request = _build(password="hunter2")

    assert "hunter2" not in repr(request)
    assert "hunter2" not in str(request)


def _build_certificate_import(tmp_path: object, **overrides: object):
    cert_path = tmp_path / "cert.pfx"  # type: ignore[operator]
    cert_path.write_bytes(b"not a real pfx")
    defaults: dict[str, object] = {
        "certificate_path": str(cert_path),
        "certificate_password": "hunter2",
        "stored_certificate_password": None,
    }
    defaults.update(overrides)
    return build_certificate_import_request(**defaults)  # type: ignore[arg-type]


def test_certificate_import_blank_path_raises(tmp_path: object) -> None:
    with pytest.raises(CertificateFormError):
        _build_certificate_import(tmp_path, certificate_path="   ")


def test_certificate_import_missing_file_raises(tmp_path: object) -> None:
    with pytest.raises(CertificateFormError):
        build_certificate_import_request(
            certificate_path=str(tmp_path) + "/does-not-exist.pfx",  # type: ignore[operator]
            certificate_password="hunter2",
            stored_certificate_password=None,
        )


def test_certificate_import_blank_password_with_no_stored_password_raises(
    tmp_path: object,
) -> None:
    with pytest.raises(CertificateFormError):
        _build_certificate_import(
            tmp_path, certificate_password="", stored_certificate_password=None
        )


def test_certificate_import_blank_password_falls_back_for_the_same_certificate(
    tmp_path: object,
) -> None:
    cert_path = str(tmp_path / "cert.pfx")  # type: ignore[operator]

    _certificate_path, certificate_password = _build_certificate_import(
        tmp_path,
        certificate_password="",
        stored_certificate_password="stored-secret",
        stored_certificate_path=cert_path,
    )

    assert certificate_password.get_secret_value() == "stored-secret"


def test_certificate_import_blank_password_is_not_reused_for_a_different_certificate(
    tmp_path: object,
) -> None:
    """The keyring holds one certificate password, not one per file. Silently feeding a
    previous certificate's password to a newly chosen `.pfx` makes `certutil` fail with
    an opaque exit code, and the path is then never remembered — the operator sees the
    old path reappear with no explanation."""
    other = tmp_path / "previously-imported.pfx"  # type: ignore[operator]
    other.write_bytes(b"not a real pfx")

    with pytest.raises(CertificateFormError):
        _build_certificate_import(
            tmp_path,
            certificate_password="",
            stored_certificate_password="stored-secret",
            stored_certificate_path=str(other),
        )


def test_certificate_import_path_match_ignores_case_and_separators(tmp_path: object) -> None:
    """Windows paths — the remembered string may differ in case or separator from what
    the file dialog or a hand-typed box produced for the very same file."""
    cert_path = str(tmp_path / "cert.pfx")  # type: ignore[operator]

    _certificate_path, certificate_password = _build_certificate_import(
        tmp_path,
        certificate_password="",
        stored_certificate_password="stored-secret",
        stored_certificate_path=cert_path.upper().replace("\\", "/"),
    )

    assert certificate_password.get_secret_value() == "stored-secret"


def test_certificate_import_typed_password_takes_priority_over_stored_password(
    tmp_path: object,
) -> None:
    _certificate_path, certificate_password = _build_certificate_import(
        tmp_path, certificate_password="typed-secret", stored_certificate_password="stored-secret"
    )

    assert certificate_password.get_secret_value() == "typed-secret"
