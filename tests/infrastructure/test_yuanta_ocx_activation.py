"""Real (non-mocked) OCX activation — exercises the actual production adapter code
against the real vendor `.ocx` files, with placeholder credentials, over a real
network round-trip to the vendor's TEST endpoint(s).

Verified working end-to-end this session (2026-08-16) — see
`docs/adr/0004-broker-session-architecture.md`'s "Execution attempt findings".

Opt-in only (`TFX_QUANT_OCX_ACTIVATION_TEST=1`) — even though it needs no real
credentials, a normal `pytest` run shouldn't silently make an external network call
just because the vendor files happen to be present on the machine. Also skipped
automatically wherever the vendor `.ocx` files aren't present at all (they're
gitignored/proprietary — see `docs/secrets-management.md`), unlike
`test_yuanta_live_smoke.py` (which needs *real* credentials and asserts a full
session-ready outcome). These tests do **not** assert login success — placeholder
credentials are expected to fail authentication; a real, well-formed failure response
is still a fully valid, meaningful round-trip proving the whole activation/
registration/invocation/event-delivery chain works.
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from collections.abc import Generator
from ctypes import wintypes

import pytest
from pydantic import SecretStr

from tfx_quant.infrastructure.yuanta import com_registration

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("TFX_QUANT_OCX_ACTIVATION_TEST") != "1",
        reason="opt-in only: set TFX_QUANT_OCX_ACTIVATION_TEST=1 to run (makes a "
        "real network call to the vendor's test endpoint)",
    ),
    pytest.mark.skipif(
        struct.calcsize("P") == 8, reason="requires an x32 (32-bit) Python interpreter"
    ),
]


@pytest.fixture(scope="module")
def wx_app() -> Generator[object, None, None]:
    """`OcxHost` hosts each control in a `wx.Frame` — a `wx.App` must exist first,
    exactly like the real desktop app (composition happens after `app.py`'s
    `wx.App()`). `wx.App` is a process-wide singleton, so this is shared across every
    test in this module rather than created per-test."""
    import wx

    app = wx.App(False)
    com_registration.register_all_per_user()
    yield app


def _pump_windows_messages(*, seconds: float) -> None:
    user32 = ctypes.windll.user32
    msg = wintypes.MSG()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.05)


# -- Trading OCX ----------------------------------------------------------------


class _RecordingTradeOrchestrator:
    """Duck-typed stand-in for `BrokerSessionOrchestrator` — records exactly the one
    callback this test cares about, `handle_trade_login_result`, instead of driving
    the full login/query/subscribe sequence (which needs real credentials)."""

    def __init__(self) -> None:
        self.login_results: list[tuple[int, int, str, str, str]] = []

    def handle_trade_login_result(
        self, generation: int, tlink_status: int, acc_list: str, casq: str, cast: str
    ) -> None:
        self.login_results.append((generation, tlink_status, acc_list, casq, cast))


@pytest.mark.skipif(
    not os.path.exists(com_registration.TRADE_OCX_PATH_32BIT),
    reason=(
        f"vendor trading OCX not present at {com_registration.TRADE_OCX_PATH_32BIT} "
        "(proprietary, not committed — see docs/secrets-management.md)"
    ),
)
def test_real_trade_ocx_activates_and_completes_a_real_login_round_trip(wx_app: object) -> None:
    from tfx_quant.application.settings.trading_settings import Environment
    from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
    from tfx_quant.infrastructure.yuanta.trade_ocx_adapter import YuantaTradeOcxAdapter

    adapter = YuantaTradeOcxAdapter(environment=Environment.TEST)
    orchestrator = _RecordingTradeOrchestrator()
    adapter.bind_orchestrator(orchestrator)  # type: ignore[arg-type]

    credentials = BrokerCredentials(user_id="TESTUSER", password=SecretStr("TESTPASS"))
    adapter.connect(credentials, generation=1)

    _pump_windows_messages(seconds=15)

    assert len(orchestrator.login_results) >= 1, (
        "no OnLogonS event received within 15s — either the network round-trip to "
        "apitest.yuantafutures.com.tw failed, or activation regressed"
    )
    generation, tlink_status, _acc_list, _casq, _cast = orchestrator.login_results[0]
    assert generation == 1
    assert isinstance(tlink_status, int)

    adapter.disconnect()


# -- Quote OCX --------------------------------------------------------------------


class _RecordingQuoteOrchestrator:
    def __init__(self) -> None:
        self.status_changes: list[tuple[int, int, str]] = []

    def handle_quote_status_changed(self, generation: int, status: int, msg: str) -> None:
        self.status_changes.append((generation, status, msg))

    def handle_quote_registration_error(
        self, generation: int, symbol: str, mode: int, error_code: int
    ) -> None:
        pass


@pytest.mark.skipif(
    not os.path.exists(com_registration.QUOTE_OCX_PATH),
    reason=(
        f"vendor quote OCX not present at {com_registration.QUOTE_OCX_PATH} "
        "(proprietary, not committed — see docs/secrets-management.md)"
    ),
)
def test_real_quote_ocx_activates_and_accepts_a_logon_call(wx_app: object) -> None:
    """Weaker assertion than the trade OCX test above, deliberately: this session
    confirmed activation + `SetMktLogon` invocation succeed without error when hosted
    in a window (unlike headless `CreateObject`, which raised `E_UNEXPECTED` for this
    control specifically), but never observed `OnMktStatusChange` fire within 20s
    using placeholder credentials — unconfirmed whether that's because the quote API
    needs a separate, real market-data agreement to respond at all (see
    `infrastructure/yuanta/README.md`) or an unresolved gap. Only asserts the call
    itself doesn't raise — see `docs/adr/0004-broker-session-architecture.md`'s
    "Execution attempt findings" for the full, honest account."""
    from tfx_quant.application.settings.trading_settings import Environment
    from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
    from tfx_quant.infrastructure.yuanta.quote_ocx_adapter import YuantaQuoteOcxAdapter

    adapter = YuantaQuoteOcxAdapter(environment=Environment.TEST)
    orchestrator = _RecordingQuoteOrchestrator()
    adapter.bind_orchestrator(orchestrator)  # type: ignore[arg-type]

    credentials = BrokerCredentials(user_id="TESTUSER", password=SecretStr("TESTPASS"))
    adapter.connect(credentials, generation=1)  # must not raise

    _pump_windows_messages(seconds=5)

    adapter.disconnect()
