"""SparkHistoricalPriceQueryAdapter — the real `HistoricalPriceQueryPort` implementation
(Feature 04 extension: 元大 SPARK API `GetKLine` 60分K history query, see
`docs/adr/0007-two-month-bar-history-persistence.md`'s extension decision).

Not independently exercised against a real login this session (no vendor DLL/.NET 8 SDK
available in this environment — same honest status `spark_api_adapter.py` already
documents). `BarHistoryBackfillService`'s own gap-detection/chunking/write logic has full
unit-test coverage via a fake `HistoricalPriceQueryPort`, independent of this file.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Sequence
from datetime import date
from typing import Any

from tfx_quant.application.ports.historical_price_query import (
    HistoricalPriceQueryError,
    VendorKLineBar,
)
from tfx_quant.domain.account import TradingAccount
from tfx_quant.infrastructure.yuanta.errors import MarketDataParseError
from tfx_quant.infrastructure.yuanta.market_data_parsing import parse_kline_bar
from tfx_quant.infrastructure.yuanta.spark_api_adapter import SparkApiSessionAdapter

_DEFAULT_RESPONSE_TIMEOUT_SECONDS = 10.0


class SparkHistoricalPriceQueryAdapter:
    """Implements `application.ports.historical_price_query.HistoricalPriceQueryPort`
    against a live `SparkApiSessionAdapter`.

    Serializes every `GetKLine` call behind one lock — matches the vendor's own
    documented ≤1/sec rate cap for this function — and turns the async `OnResponse`
    callback into the port's synchronous, blocking-with-timeout contract using a
    single-slot queue: with calls fully serialized, "the next `GetKLine` response
    belongs to the call just made" is a safe assumption, no per-request correlation ID
    needed. The queue is drained before each call to avoid a stale response from a
    previously-timed-out call being mistaken for the current one — this narrows, but
    does not fully eliminate, that race; acceptable given no real vendor login has ever
    exercised this path (see module docstring)."""

    def __init__(
        self,
        session_adapter: SparkApiSessionAdapter,
        *,
        response_timeout_seconds: float = _DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> None:
        self._session_adapter = session_adapter
        self._timeout = response_timeout_seconds
        self._call_lock = threading.Lock()
        self._pending: queue.Queue[Any] = queue.Queue(maxsize=1)
        session_adapter.bind_kline_handler(self._pending.put)

    def query_60m_kline(
        self,
        *,
        account: TradingAccount,
        vendor_symbol: str,
        start_date: date,
        end_date: date,
    ) -> Sequence[VendorKLineBar]:
        with self._call_lock:
            self._drain_pending()
            ok = self._session_adapter.request_kline(account, vendor_symbol, start_date, end_date)
            if not ok:
                raise HistoricalPriceQueryError(
                    f"GetKLine 呼叫失敗（{vendor_symbol} {start_date}~{end_date}）"
                )
            try:
                obj_value = self._pending.get(timeout=self._timeout)
            except queue.Empty as exc:
                raise HistoricalPriceQueryError(
                    f"GetKLine 查詢逾時，未收到回應（{vendor_symbol} {start_date}~{end_date}）"
                ) from exc
        return _parse_kline_result(obj_value)

    def _drain_pending(self) -> None:
        try:
            while True:
                self._pending.get_nowait()
        except queue.Empty:
            return


def _parse_kline_result(obj_value: Any) -> list[VendorKLineBar]:
    bars: list[VendorKLineBar] = []
    for item in obj_value.KLineList:
        timestamp = item.TimeStamp
        try:
            parsed = parse_kline_bar(
                year=timestamp.Year,
                month=timestamp.Month,
                day=timestamp.Day,
                hour=timestamp.Hour,
                minute=timestamp.Minute,
                second=timestamp.Second,
                open_price=item.OpenPrice,
                high_price=item.HighPrice,
                low_price=item.LowPrice,
                close_price=item.ClosePrice,
                deal_vol=item.DealVol,
            )
        except MarketDataParseError:
            # One malformed row must never discard the rest of an otherwise-valid
            # batch — see market_data_parsing.parse_kline_bar's docstring.
            continue
        bars.append(
            VendorKLineBar(
                at=parsed.at,
                open=parsed.open,
                high=parsed.high,
                low=parsed.low,
                close=parsed.close,
                volume=parsed.volume,
            )
        )
    return bars
