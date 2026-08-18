"""Opt-in live smoke test against the real Yuanta test environment.

Skipped by default. Requires explicit opt-in (`TFX_QUANT_LIVE_YUANTA_SMOKE_TEST=1`)
**and** real, resolvable credentials (`TFX_QUANT_YUANTA_USER_ID` env var + a Windows
Credential Manager entry — see `docs/secrets-management.md`), per the implementation
prompt's "以 staging/mock 提供登入 smoke test；真實帳號測試必須是明確 opt-in".

Order submission has no code path anywhere in this repository yet (`SendOrderF` is
never called — see `infrastructure/yuanta/README.md`; order submission is Feature
06's job), so "預設禁止送單" is satisfied structurally rather than by a runtime flag
guarding functionality that doesn't exist.

Also requires both OCXs to actually be loadable/registered. The project's sole venv is
x32 (32-bit — see ADR 0001), so the `struct.calcsize` skip below is a defensive
fallback rather than the live blocker; the real blocker as of this writing is that
`YuantaOrd.ocx` itself won't `LoadLibrary` on this dev machine at all (an unrelated
legacy VC90 MFC runtime issue, not a bitness one) — see
`docs/adr/0004-broker-session-architecture.md`'s "Execution attempt findings" and
"What's not verified".
"""

from __future__ import annotations

import os
import struct

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TFX_QUANT_LIVE_YUANTA_SMOKE_TEST") != "1",
    reason="opt-in only: set TFX_QUANT_LIVE_YUANTA_SMOKE_TEST=1 to run against the "
    "real Yuanta test environment with real credentials",
)


def test_real_session_reaches_ready_against_the_test_environment() -> None:
    if struct.calcsize("P") == 8:
        pytest.skip("requires an x32 (32-bit) Python interpreter — see ADR 0001")

    from tfx_quant.application.events.event_coordinator import EventCoordinator
    from tfx_quant.application.settings.trading_settings import Environment
    from tfx_quant.infrastructure.yuanta.credentials import EnvironmentAndKeyringCredentialSource
    from tfx_quant.infrastructure.yuanta.preflight import raise_if_any_failed, run_preflight_checks
    from tfx_quant.infrastructure.yuanta.quote_ocx_adapter import YuantaQuoteOcxAdapter
    from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator
    from tfx_quant.infrastructure.yuanta.trade_ocx_adapter import YuantaTradeOcxAdapter

    credential_source = EnvironmentAndKeyringCredentialSource()
    raise_if_any_failed(run_preflight_checks(credential_source))

    event_coordinator = EventCoordinator()
    event_coordinator.start()
    try:
        trade_adapter = YuantaTradeOcxAdapter(environment=Environment.TEST)
        quote_adapter = YuantaQuoteOcxAdapter(environment=Environment.TEST)
        orchestrator = BrokerSessionOrchestrator(
            trade_adapter=trade_adapter,
            quote_adapter=quote_adapter,
            credential_source=credential_source,
            event_coordinator=event_coordinator,
            login_timeout_seconds=30.0,
        )
        trade_adapter.bind_orchestrator(orchestrator)
        quote_adapter.bind_orchestrator(orchestrator)

        orchestrator.start()

        import time

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not orchestrator.capabilities.is_session_ready:
            time.sleep(0.5)

        try:
            assert orchestrator.capabilities.is_session_ready, (
                f"session did not reach ready within 60s; capabilities={orchestrator.capabilities}"
            )
        finally:
            orchestrator.stop()
    finally:
        event_coordinator.stop(timeout=5.0)
