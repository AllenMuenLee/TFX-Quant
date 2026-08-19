"""Opt-in live smoke test against the real Yuanta SPARK API test environment.

Skipped by default. Requires explicit opt-in (`TFX_QUANT_LIVE_YUANTA_SMOKE_TEST=1`)
**and** real, resolvable credentials — a test-only `TFX_QUANT_YUANTA_TEST_USER_ID` env
var for the full SPARK API account string, plus a password already secure-stored for
that ID via the real login screen's "安全儲存密碼" checkbox (or `keyring set
tfx_quant.yuanta <user_id>` directly) — per the implementation prompt's "以 staging/
mock 提供登入 smoke test；真實帳號測試必須是明確 opt-in". This deliberately reuses
`credentials.load_stored_password`, the same production code path the login screen
uses, rather than a separate test-only credential loader — see
`docs/secrets-management.md`.

Order submission has no code path anywhere in this repository yet (`SendFutureOrder` is
never called — see `infrastructure/yuanta/README.md`; order submission is Feature 06's
job), so "預設禁止送單" is satisfied structurally rather than by a runtime flag guarding
functionality that doesn't exist.

Also requires the .NET 8 SDK and the real `YuantaSparkAPI.dll` to be present (see
`preflight.py`) — `raise_if_any_failed(run_preflight_checks())` below skips the test
via a normal exception if they aren't, rather than a bespoke check here. Unlike the
retired legacy-OCX build (which did get a real end-to-end round-trip verified in an
earlier session), this SPARK API rewrite has not yet been run against a live server in
any session — see `docs/adr/0004-broker-session-architecture.md`'s "What's not
verified". This test is what closes that gap whenever someone with a real environment
runs it.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TFX_QUANT_LIVE_YUANTA_SMOKE_TEST") != "1",
    reason="opt-in only: set TFX_QUANT_LIVE_YUANTA_SMOKE_TEST=1 to run against the "
    "real Yuanta SPARK API test environment with real credentials",
)


def test_real_session_reaches_ready_against_the_test_environment() -> None:
    user_id = os.environ.get("TFX_QUANT_YUANTA_TEST_USER_ID")
    if not user_id:
        pytest.skip("set TFX_QUANT_YUANTA_TEST_USER_ID to the SPARK API account to smoke-test with")

    from pydantic import SecretStr

    from tfx_quant.application.events.event_coordinator import EventCoordinator
    from tfx_quant.application.ports.broker_session import LoginRequest
    from tfx_quant.application.settings.trading_settings import Environment
    from tfx_quant.infrastructure.yuanta import credentials
    from tfx_quant.infrastructure.yuanta.preflight import raise_if_any_failed, run_preflight_checks
    from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator
    from tfx_quant.infrastructure.yuanta.spark_api_adapter import SparkApiSessionAdapter

    password = credentials.load_stored_password(user_id)
    if not password:
        pytest.skip(
            f"no password secure-stored for {user_id!r} — use the login screen's "
            "「安全儲存密碼」checkbox or `keyring set tfx_quant.yuanta <user_id>` first"
        )

    raise_if_any_failed(run_preflight_checks())

    event_coordinator = EventCoordinator()
    event_coordinator.start()
    try:
        adapter = SparkApiSessionAdapter()
        orchestrator = BrokerSessionOrchestrator(
            adapter=adapter,
            event_coordinator=event_coordinator,
            login_timeout_seconds=30.0,
        )
        adapter.bind_orchestrator(orchestrator)

        orchestrator.start(
            LoginRequest(
                environment=Environment.TEST,
                user_id=user_id,
                password=SecretStr(password),
            )
        )

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
