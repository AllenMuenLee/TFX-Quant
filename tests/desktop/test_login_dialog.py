"""Tests for `desktop/login_dialog.py`'s pure, wx-free `build_login_request` — the
validation/DTO-building logic split out specifically so it's testable without a live
`wx.App`, the same way `BrokerSessionOrchestrator` is split from its pythonnet/wx glue.
`LoginDialog` itself (the `wx.Dialog` shell) has no test here, matching this codebase's
existing convention of not unit-testing wx widgets directly.
"""

from __future__ import annotations

import pytest

from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.desktop.login_dialog import LoginFormError, build_login_request


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
