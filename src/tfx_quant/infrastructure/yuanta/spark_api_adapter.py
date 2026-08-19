"""SparkApiSessionAdapter — the real, `pythonnet`-backed `SparkAdapterPort`.

Wraps `spark_client.SparkApiClient` and translates its unified `OnResponse(intMark,
dwIndex, strIndex, objHandle, objValue)` dispatch (基礎 > 回應事件) into
`BrokerSessionOrchestrator.handle_*` calls, extracting only primitive values from the
`.NET` objects before crossing back into pure-Python `session_orchestrator.py` — same
"event sink does the minimum, real processing happens on the orchestrator" split the
retired legacy OCX adapters used (see `docs/adr/0004-broker-session-architecture.md`).

Not independently exercised against a real login this session (no vendor DLL/.NET 8 SDK
available in this environment) — see ADR 0004's "What's not verified" for the honest
status. `BrokerSessionOrchestrator`'s own sequencing/retry/capability logic has full
unit-test coverage via a fake `SparkAdapterPort`, independent of this file.
"""

from __future__ import annotations

from typing import Any

from tfx_quant.application.settings.trading_settings import Environment
from tfx_quant.domain.account import TradingAccount
from tfx_quant.infrastructure.yuanta.credentials import BrokerCredentials
from tfx_quant.infrastructure.yuanta.errors import MarketDataSubscriptionError, YuantaSessionError
from tfx_quant.infrastructure.yuanta.session_orchestrator import BrokerSessionOrchestrator
from tfx_quant.infrastructure.yuanta.spark_client import SparkApiClient


def account_to_spark_string(account: TradingAccount) -> str:
    """Reconstructs SPARK API's `F + 分公司代號(7+3) + 帳號(7)` account string from a
    `TradingAccount` — the inverse of `session_orchestrator._parse_login_accounts`'s
    split. Every SPARK API call that takes an `Account`/`LoginAcno` parameter
    (`Login`, `GetRealReport`, `GetFutStoreSummary`, `SubscribeStockTick`) needs this
    exact string, not the split `branch_id`/`account_no` fields."""
    return f"F{account.branch_id}{account.account_no}"


class SparkApiSessionAdapter:
    """Implements `session_orchestrator.SparkAdapterPort`."""

    def __init__(self) -> None:
        self._client: SparkApiClient | None = None
        self._orchestrator: BrokerSessionOrchestrator | None = None
        self._generation = 0

    def bind_orchestrator(self, orchestrator: BrokerSessionOrchestrator) -> None:
        """Wires the callback direction; `composition.py` calls this once, right
        after constructing both the orchestrator and this adapter (a real dependency
        cycle — orchestrator calls out to the adapter, the adapter calls back into the
        orchestrator — resolved via this explicit two-step wiring instead of a
        constructor cycle)."""
        self._orchestrator = orchestrator

    # -- SparkAdapterPort -------------------------------------------------------------

    def open_and_login(
        self, credentials: BrokerCredentials, *, generation: int, environment: Environment
    ) -> None:
        self._generation = generation
        try:
            self._client = SparkApiClient()
        except Exception as exc:  # pragma: no cover - needs real .NET 8 SDK/DLL
            raise YuantaSessionError(
                f"無法初始化元大 SPARK API 元件：{exc}。"
                "請確認已安裝 .NET 8 SDK 且 YuantaSparkAPI 元件檔案存在。"
            ) from exc
        self._client.add_response_handler(self._on_response)
        self._client.open(environment)
        self._client.login(credentials.user_id, credentials.password.get_secret_value())

    def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            self._client.logout()
            self._client.close()
        finally:
            self._client.dispose()
            self._client = None

    def query_real_report(self, account: TradingAccount) -> None:
        assert self._client is not None
        self._client.get_real_report(account_to_spark_string(account))

    def query_positions(self, account: TradingAccount) -> None:
        assert self._client is not None
        self._client.get_fut_store_summary(account_to_spark_string(account))

    def subscribe(self, symbol: str, account: TradingAccount) -> None:
        assert self._client is not None
        ok = self._client.subscribe_stock_tick(account_to_spark_string(account), [symbol])
        if not ok:
            raise MarketDataSubscriptionError(f"商品 {symbol} 行情訂閱呼叫失敗")

    def unsubscribe(self, symbol: str, account: TradingAccount) -> None:
        assert self._client is not None
        self._client.unsubscribe_stock_tick(account_to_spark_string(account), [symbol])

    # -- OnResponse dispatch --------------------------------------------------------

    def _on_response(
        self, int_mark: int, dw_index: int, str_index: str, obj_handle: object, obj_value: Any
    ) -> None:
        """Every branch below does the minimum possible — pull primitive fields out of
        the `.NET` object and hand straight to the orchestrator. No parsing/business
        logic lives here (`market_data_parsing.py`/`session_orchestrator.py` own that),
        matching the implementation prompt's "callback 不阻塞；callback 內容立即複製為
        內部 DTO"."""
        assert self._orchestrator is not None
        generation = self._generation

        if int_mark == 1:  # 查詢回應資訊 (query response)
            if str_index == "Login":
                self._handle_login_response(generation, obj_value)
            elif str_index == "GetRealReport":
                self._orchestrator.handle_real_report_query_result(generation)
            elif str_index == "GetFutStoreSummary":
                self._orchestrator.handle_position_query_result(generation)
            return

        if int_mark == 2:  # 訂閱回應資訊 (subscription push)
            if str_index == "SubscribeStockTick":
                self._handle_stock_tick_push(generation, obj_value)
            # str_index == "RR_RealReport": 即時回報推播 (order/fill push). Feature 02
            # only needs the report *capability* (true as soon as login succeeds, since
            # SPARK API auto-subscribes on login — see session_orchestrator.py's
            # `_current_capabilities`). Parsing the pushed `RealReport` into a typed
            # `Order`/`Fill` is Feature 06's job, not wired up here.
            return

    def _handle_login_response(self, generation: int, login_result: Any) -> None:
        status = login_result.LoginStatus
        msg_code = str(status.MsgCode)
        msg_content = str(status.MsgContent)
        entries = tuple(
            (str(item.Account), str(item.Name), str(item.InvestorID), str(item.SellerNo))
            for item in login_result.LoginList
        )
        assert self._orchestrator is not None
        self._orchestrator.handle_login_result(generation, msg_code, msg_content, entries)

    def _handle_stock_tick_push(self, generation: int, tick_result: Any) -> None:
        time_obj = tick_result.Time
        assert self._orchestrator is not None
        self._orchestrator.handle_market_data_push(
            generation,
            str(tick_result.StkCode),
            int(tick_result.SerialNo),
            tick_result.DealPrice,
            tick_result.DealVol,
            int(time_obj.bytHour),
            int(time_obj.bytMin),
            int(time_obj.bytSec),
            int(time_obj.ushtMSec),
        )
