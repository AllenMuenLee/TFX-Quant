from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tfx_quant.application.settings.trading_settings import validate_startup
from tfx_quant.desktop.__main__ import _parse_args
from tfx_quant.domain.market_data import RawMarketEvent
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp
from tfx_quant.simulation.clock import VirtualClock
from tfx_quant.simulation.composition import build_simulation_services
from tfx_quant.simulation.market_data_control import (
    SimulationDataSource,
    SimulationMarketDataController,
)
from tfx_quant.simulation.replay import (
    ReplayEvent,
    ReplayHarness,
    ReplayMetadata,
    ReplaySource,
)


def _at(hour: int, minute: int) -> Timestamp:
    return Timestamp(datetime(2026, 8, 25, hour, minute, tzinfo=TAIPEI_TZ))


def _event(sequence: int, at: Timestamp) -> RawMarketEvent:
    return RawMarketEvent(
        symbol="MXF202609",
        sequence=sequence,
        session_id="fixture-session",
        received_at=at,
        fields={
            "MatchTime": at.value.strftime("%H%M%S"),
            "MatchPri": "18500",
            "MatchQty": "1",
            "TolMatchQty": str(sequence),
        },
    )


def test_mock_cli_flag_is_not_treated_as_a_settings_path() -> None:
    path, mock = _parse_args(["--mock"])
    assert path.name == "settings.example.json"
    assert mock is True


def test_jump_delivers_every_event_before_advancing_to_target() -> None:
    clock = VirtualClock(_at(10, 0))
    first, second = _event(1, _at(10, 1)), _event(2, _at(10, 2))
    replay = ReplayHarness(
        ReplayMetadata("ordered-jump", "1", 42, ReplaySource.TEST_FIXTURE),
        clock,
        [ReplayEvent(second.received_at, second), ReplayEvent(first.received_at, first)],
    )
    seen: list[int] = []

    assert replay.jump_to(_at(10, 5), lambda event: seen.append(event.sequence)) == 2
    assert seen == [1, 2]
    assert clock.now() == _at(10, 5)
    assert replay.pending_count == 0


def test_virtual_clock_refuses_backwards_jump() -> None:
    clock = VirtualClock(_at(10, 0))
    try:
        clock.advance_to(_at(9, 59))
    except ValueError as exc:
        assert "backwards" in str(exc)
    else:
        raise AssertionError("backwards jump was accepted")


class _FakeQuoteRuntime:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.stop_count = 0

    def stop(self) -> None:
        self.stop_count += 1

    def start(self, user_id: str, password: Any) -> None:
        self.started.append((user_id, password.get_secret_value()))


def test_market_data_source_switch_does_not_require_a_broker_login() -> None:
    controller = SimulationMarketDataController()
    runtime = _FakeQuoteRuntime()
    controller.attach(runtime)  # type: ignore[arg-type]

    controller.use_mock_data()
    controller.use_real_data("quote-user", "quote-password")

    assert runtime.started == [
        ("SIMULATION", "SIMULATION-ONLY"),
        ("quote-user", "quote-password"),
    ]
    assert runtime.stop_count == 2
    assert controller.source == SimulationDataSource.REAL_YUANTA_QUOTES.value


@pytest.mark.parametrize(("user_id", "password"), [("", "x"), ("user", "")])
def test_real_quote_source_requires_quote_credentials(user_id: str, password: str) -> None:
    controller = SimulationMarketDataController()
    controller.attach(_FakeQuoteRuntime())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        controller.use_real_data(user_id, password)


def test_simulation_composition_never_runs_trade_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    valid_settings_raw: dict[str, Any],
) -> None:
    import tfx_quant.desktop.composition as composition

    def forbidden_preflight() -> list[object]:
        raise AssertionError("real trade preflight was reached")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(composition, "run_preflight_checks", forbidden_preflight)

    services = build_simulation_services(validate_startup(valid_settings_raw))

    assert services.simulation is True
    assert services.simulation_market_data is not None
    assert services.trade_gateway.is_logged_in() is True
