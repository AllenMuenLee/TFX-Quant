from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from tfx_quant.application.ports.broker_session import LoginRequest
from tfx_quant.application.settings.trading_settings import Environment, validate_startup
from tfx_quant.desktop.composition import build_services, compute_readiness, load_settings
from tfx_quant.domain.instrument import Instrument
from tfx_quant.infrastructure.yuanta.errors import PreflightCheckFailed


def _login_request() -> LoginRequest:
    return LoginRequest(
        environment=Environment.TEST,
        user_id="F00000000012345678",
        password=SecretStr("x"),
    )


def test_load_settings_reads_and_validates_json(
    tmp_path: Path, valid_settings_raw: dict[str, Any]
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(valid_settings_raw), encoding="utf-8")

    settings = load_settings(settings_path)

    assert settings.account_alias == "primary"


def test_example_settings_file_is_itself_valid() -> None:
    example_path = (
        Path(__file__).parents[2] / "src" / "tfx_quant" / "desktop" / "settings.example.json"
    )
    settings = load_settings(example_path)
    assert settings.use_mock is True


def test_build_services_wires_mock_gateways_when_use_mock_true(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert services.trade_gateway.is_logged_in() is False
    assert services.quote_gateway.is_market_data_valid() is False


def test_build_services_wires_a_broker_session_when_use_mock_true(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    assert services.broker_session.capabilities.login is False

    services.broker_session.start(_login_request())

    assert services.broker_session.capabilities.is_session_ready is True
    readiness = dict(compute_readiness(services))
    assert readiness["Broker session: login"] is True
    assert readiness["Broker session: market data"] is True
    assert readiness["Broker session: trading"] is True
    assert readiness["Broker session: order reports"] is True
    assert readiness["Broker session: queries"] is True


def test_build_services_raises_actionable_preflight_error_when_use_mock_false(
    valid_settings_raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real branch must fail loudly at startup with an aggregated, actionable
    message for whatever preflight check fails, never silently fall back to a fake —
    per docs/adr/0004-broker-session-architecture.md. The DLL-directory check is forced
    to fail here (pointed at an empty tmp dir, guaranteed to lack `YuantaSparkAPI.dll`)
    so the assertion is deterministic regardless of host state. Credentials are no
    longer checked at startup at all (they're entered on the login screen, not
    available until then — see docs/secrets-management.md)."""
    import tfx_quant.infrastructure.yuanta.preflight as preflight

    monkeypatch.setattr(preflight, "default_dll_directory", lambda: tmp_path)

    valid_settings_raw["use_mock"] = False
    settings = validate_startup(valid_settings_raw)

    with pytest.raises(PreflightCheckFailed) as exc_info:
        build_services(settings)

    message = str(exc_info.value)
    assert "password" not in message.lower()
    assert "hunter2" not in message


def test_compute_readiness_never_includes_account_number_or_secrets(
    valid_settings_raw: dict[str, Any],
) -> None:
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)
    readiness = compute_readiness(services)
    labels = " ".join(label for label, _ in readiness)
    assert "password" not in labels.lower()
    assert "account_no" not in labels.lower()


def test_market_data_bar_service_is_wired_as_the_real_bar_signal_state_store(
    valid_settings_raw: dict[str, Any],
) -> None:
    """ADR 0005 left `NullBarSignalStateStore` as a documented placeholder — Feature 04
    must wire the real service in its place, so switching instruments actually resets
    (activates) the bar aggregator for the newly-selected contract."""
    settings = validate_startup(valid_settings_raw)
    services = build_services(settings)

    resolved = services.instrument_selection.resolve_near_month(Instrument.MXF)
    services.instrument_selection.switch_to(resolved)

    bar_service = services.market_data_bar_service
    assert bar_service.forming_bar(resolved.instrument, resolved.contract) is None
    assert bar_service.recent_closed_bars(resolved.instrument, resolved.contract) == ()
    assert bar_service.is_stale(resolved.instrument, resolved.contract) is True
    assert bar_service.has_gap(resolved.instrument, resolved.contract) is False
