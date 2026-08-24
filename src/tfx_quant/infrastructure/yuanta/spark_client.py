"""SparkApiClient — thin `pythonnet` wrapper around `YuantaSparkAPI.dll` (元大 SPARK API).

Isolates every `pythonnet`/`clr`/`YuantaOneAPI` import to this one module — the same
"only infrastructure.yuanta may import vendor types" boundary the retired legacy OCX
adapters kept (see `docs/adr/0002-ui-framework-and-com-hosting.md`). Nothing outside
this file (and `spark_api_adapter.py`, which wraps it in the shape
`session_orchestrator.SparkAdapterPort` expects) touches `clr`/`YuantaOneAPI` directly.

Deliberately thin: every method is a direct, uncached call into the real
`YuantaSparkAPITrader` object the official docs describe (基礎/交易/行情/帳務/回報
pages — see `infrastructure/yuanta/README.md`) — no retry, no parsing, no business
logic. `session_orchestrator.py` owns all sequencing.

Only what Features 01–02 need is wired up here: session lifecycle (`Open`/`Login`/
`LogOut`/`Close`/`Dispose`) and the two pre-ready safety queries (`GetRealReport`,
`GetFutStoreSummary`). There is no market-data method here at all — market data comes
entirely from the third-party `yfinance` package (`infrastructure.market_data.
yfinance_history_adapter`), never this vendor client; a `SubscribeStockTick`/
`UnSubscribeStockTick` wrapper existed here in an earlier revision and was removed once
the implementation prompt was rewritten to forbid any Yuanta/SPARK market-data path, not
left in place unused (see `implementation prompt/00-spark-to-futures-api-migration/
implementation-prompt.md`). `SendFutureOrder` (order placement) is Feature 06's job and
deliberately has no method here yet — adding it without a real order/fill state machine
to drive it would be exactly the kind of forward-scope creep the project's conventions
warn against.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tfx_quant.application.settings.trading_settings import Environment

OnResponseCallback = Callable[[int, int, str, Any, Any], None]
"""Matches the docs' own `on_response(intMark, dwIndex, strIndex, objHandle,
objValue)` signature exactly (基礎 > 回應事件). `intMark`: 0=系統回應, 1=查詢回應,
2=訂閱推播. `objValue`'s shape depends on `strIndex` (e.g. `'Login'` ->
`LoginResult`, `'GetRealReport'` -> `RealReportResult`, `'SubscribeStockTick'` (push)
-> `StockTickResult`) — kept `Any` here, `spark_api_adapter.py` narrows per `strIndex`."""


def default_dll_directory() -> Path:
    """Conventional local install path for the SPARK API component — mirrors the
    retired legacy OCX adapters' `C:\\Yuanta\\API`/`C:\\Yuanta\\QAPI` convention (see
    `infrastructure/yuanta/README.md`), not a vendor-documented requirement (the docs
    only say to keep every `.dll`/`.so`/`.dylib` from the downloaded component zip
    together and point `sys.path`/`os.add_dll_directory` at that folder — the exact
    folder path is this codebase's own convention, not the vendor's)."""
    return Path(r"C:\Yuanta\SparkAPI")


class SparkApiClient:
    """Wraps one `YuantaSparkAPITrader` instance for one login session's lifetime.

    Constructing this performs the real `clr.AddReference("YuantaSparkAPI")` call, so
    it requires the .NET 8 SDK and the vendor DLL to actually be present — tests must
    never construct this directly; use a fake satisfying `spark_api_adapter.py`'s
    `SparkAdapterPort` instead (see `tests/infrastructure/
    test_broker_session_orchestrator.py`).
    """

    def __init__(self, *, dll_directory: Path | None = None) -> None:
        directory = dll_directory or default_dll_directory()
        sys.path.append(str(directory))
        if sys.platform == "win32":
            os.add_dll_directory(str(directory))

        from pythonnet import load

        load("coreclr")
        import clr

        clr.AddReference("YuantaSparkAPI")
        from YuantaOneAPI import YuantaSparkAPITrader, enumEnvironmentMode, enumLogType

        self._enum_environment_mode = enumEnvironmentMode
        self._enum_log_type = enumLogType
        self._trader = YuantaSparkAPITrader()
        self._trader.SetLogType(enumLogType.COMMON)

    # -- Response dispatch ----------------------------------------------------------

    def add_response_handler(self, handler: OnResponseCallback) -> None:
        self._trader.OnResponse += handler

    def remove_response_handler(self, handler: OnResponseCallback) -> None:
        self._trader.OnResponse -= handler

    # -- Session lifecycle (基礎) -----------------------------------------------------

    def open(self, environment: Environment) -> None:
        mode = (
            self._enum_environment_mode.PROD
            if environment is Environment.PRODUCTION
            else self._enum_environment_mode.UAT
        )
        self._trader.Open(mode)

    def login(self, account: str, password: str) -> bool:
        """Windows-only 2-arg form (`Login(Account, Pass)`) — matches this codebase's
        Windows-desktop scope (see `docs/adr/0001-python-version-and-runtime.md`). The
        real login result (`LoginResult`) arrives asynchronously via `OnResponse` with
        `strIndex == 'Login'`, not this return value — see module docstring."""
        result: bool = self._trader.Login(account, password)
        return result

    def logout(self) -> bool:
        result: bool = self._trader.LogOut()
        return result

    def close(self) -> None:
        self._trader.Close()

    def dispose(self) -> None:
        self._trader.Dispose()

    # -- Safety queries (帳務 / 回報) --------------------------------------------------

    def get_real_report(self, account: str) -> bool:
        """即時回報查詢 — order/fill status query, replaces the legacy OCX API's
        separate `ReportQuery`/`DealQuery` pair with one unified call. Result arrives
        via `OnResponse` with `strIndex == 'GetRealReport'`."""
        result: bool = self._trader.GetRealReport(account)
        return result

    def get_fut_store_summary(self, account: str) -> bool:
        """期貨庫存總表查詢 — futures position query, replaces the legacy OCX API's
        undocumented `RA003` mechanism with a fully documented one. Result arrives via
        `OnResponse` with `strIndex == 'GetFutStoreSummary'`."""
        result: bool = self._trader.GetFutStoreSummary(account)
        return result
