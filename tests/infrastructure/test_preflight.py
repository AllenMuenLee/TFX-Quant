from __future__ import annotations

import struct
from pathlib import Path

import pytest
from pydantic import SecretStr

import tfx_quant.infrastructure.yuanta.preflight as preflight
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials, StaticCredentialSource
from tfx_quant.infrastructure.yuanta.errors import CredentialsUnavailableError, PreflightCheckFailed


class _FailingCredentialSource:
    def load(self) -> BrokerCredentials:
        raise CredentialsUnavailableError("找不到登入 ID")


def _working_credential_source() -> StaticCredentialSource:
    return StaticCredentialSource(
        BrokerCredentials(user_id="A123456789", password=SecretStr("hunter2"))
    )


def test_bitness_check_matches_the_interpreter_actually_running_this_test() -> None:
    """A real, meaningful assertion (not mocked): whichever interpreter runs this
    suite, the check's pass/fail must agree with its actual bitness — the quote OCX
    has no 64-bit build (see docs/adr/0001-python-version-and-runtime.md), so this is
    the one preflight check whose correct answer depends on the interpreter, not on
    the machine having (or lacking) any Yuanta component installed."""
    is_64bit = struct.calcsize("P") == 8
    checks = preflight.run_preflight_checks(_working_credential_source())
    bitness_check = next(c for c in checks if c.name == "處理程序位元數")
    assert bitness_check.passed is (not is_64bit)
    assert "32" in bitness_check.message


def test_comtypes_importable_check_passes() -> None:
    checks = preflight.run_preflight_checks(_working_credential_source())
    comtypes_check = next(c for c in checks if c.name == "comtypes 套件")
    assert comtypes_check.passed is True


def test_com_registration_check_fails_when_progid_not_registered() -> None:
    """A ProgID that is definitely never registered by anything (not a real vendor
    one, so `run_preflight_checks`'s self-registration step can't accidentally make
    this pass) must be reported as a clear failure."""
    check = preflight._check_com_registration(
        "Definitely.Not.A.Real.ProgId.Nobody.Registers.This.1", "測試元件"
    )
    assert check.passed is False
    assert "HKEY_CURRENT_USER" in check.message or "供應商安裝說明" in check.message


def test_run_preflight_checks_attempts_per_user_self_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_preflight_checks` must attempt (best-effort) per-user COM
    self-registration before checking registration status — see
    `com_registration.py`: this is what lets the real gateway work without admin
    rights, verified this session against the real vendor files."""
    calls = []
    monkeypatch.setattr(
        preflight.com_registration, "register_all_per_user", lambda: calls.append(1)
    )

    preflight.run_preflight_checks(_working_credential_source())

    assert calls == [1]


def test_run_preflight_checks_survives_self_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If self-registration itself fails (e.g. component files not present yet),
    `run_preflight_checks` must not raise — whatever downstream checks find (which
    may still pass, e.g. if a component was already registered by some other means)
    is reported normally rather than the exception propagating."""
    calls = []

    def _raise() -> None:
        calls.append(1)
        raise preflight.YuantaSessionError("找不到元件檔案")

    monkeypatch.setattr(preflight.com_registration, "register_all_per_user", _raise)

    checks = preflight.run_preflight_checks(_working_credential_source())  # must not raise

    assert calls == [1]
    assert len(checks) == 5


def test_com_registration_check_passes_when_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_ocx = tmp_path / "Fake.ocx"
    fake_ocx.write_bytes(b"not a real ocx")

    monkeypatch.setattr(preflight, "progid_to_clsid", lambda progid: "{FAKE-CLSID}")
    monkeypatch.setattr(preflight, "clsid_inproc_server_path", lambda clsid: str(fake_ocx))

    check = preflight._check_com_registration("Fake.ProgId.1", "測試元件")

    assert check.passed is True
    assert str(fake_ocx) in check.message


def test_com_registration_check_fails_when_file_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "progid_to_clsid", lambda progid: "{FAKE-CLSID}")
    monkeypatch.setattr(
        preflight, "clsid_inproc_server_path", lambda clsid: r"C:\does\not\exist.ocx"
    )

    check = preflight._check_com_registration("Fake.ProgId.1", "測試元件")

    assert check.passed is False
    assert "移動或移除" in check.message


def test_credential_check_passes_with_working_source() -> None:
    checks = preflight.run_preflight_checks(_working_credential_source())
    credential_check = next(c for c in checks if c.name == "登入憑證")
    assert credential_check.passed is True


def test_credential_check_fails_with_failing_source() -> None:
    checks = preflight.run_preflight_checks(_FailingCredentialSource())
    credential_check = next(c for c in checks if c.name == "登入憑證")
    assert credential_check.passed is False
    assert credential_check.message == "找不到登入 ID"


def test_raise_if_any_failed_aggregates_every_failure_not_just_the_first() -> None:
    checks = [
        preflight.PreflightCheck(name="檢查一", passed=False, message="原因一"),
        preflight.PreflightCheck(name="檢查二", passed=True, message="通過"),
        preflight.PreflightCheck(name="檢查三", passed=False, message="原因三"),
    ]

    with pytest.raises(PreflightCheckFailed) as exc_info:
        preflight.raise_if_any_failed(checks)

    message = str(exc_info.value)
    assert "檢查一" in message
    assert "原因一" in message
    assert "檢查三" in message
    assert "原因三" in message
    assert "檢查二" not in message  # the passing check isn't listed as a failure


def test_raise_if_any_failed_does_not_raise_when_all_passed() -> None:
    all_passed = [preflight.PreflightCheck(name="x", passed=True, message="ok")]
    preflight.raise_if_any_failed(all_passed)  # should not raise
