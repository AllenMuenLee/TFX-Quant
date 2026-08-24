"""YfinanceHistoryQueryAdapter — the real `YahooHistoryQueryPort` implementation
(Feature 04 extension: third-party `yfinance` package, see
`docs/adr/0007-two-month-bar-history-persistence.md`'s yfinance extension decision).

Only this module (and `mock_yahoo_history_query.py`, which never imports `yfinance` at
all) may import `yfinance`/`pandas` — every other layer talks to the `YahooBar`/
`YahooHistoryQueryPort` shapes only, per `application.ports.yahoo_history_query`'s
module docstring.

**Verified against the actually-installed package, not guessed.** Per this project's
own "no third-party API may be invented" rule, the parameter names, defaults, and return
shape referenced here were read directly out of this environment's installed
`yfinance` package source (`Ticker.history()` / `PriceHistory.history()` in
`yfinance/scrapers/history.py`) rather than assumed from memory — `interval="1h"` is a
documented valid interval, `start`/`end` are date-string/`datetime` inclusive/exclusive
respectively, and `auto_adjust` genuinely defaults to `True` (hence this adapter always
passes it explicitly, per the implementation prompt's "auto_adjust 等價格調整選項必須明
確設定" requirement) — see this module's `_HISTORY_KWARGS`.

**Never independently exercised against a real Yahoo Finance HTTP endpoint.** This
sandboxed environment has no live network path to Yahoo Finance, and this particular
Python virtual environment's `pandas`/`numpy` ABI mismatch (a 32-bit interpreter with no
matching binary `pandas` wheel newer than 2.0.3 for the installed `numpy` 2.x) means
`import pandas`/`import yfinance` cannot even be exercised locally here — same honest
status the vendor `spark_api_adapter.py` already carries for its own untested paths. The
exact exception hierarchy `curl_cffi`/`requests` raise for a real timeout/connection
failure through `yfinance`'s HTTP layer is therefore a best-effort mapping (`OSError`/
`ConnectionError`/`TimeoutError`/`yfinance.exceptions.YFRateLimitError`), not a verified
fact; anything outside that mapping still safely degrades to a single non-retried
`YahooHistoryQueryError` rather than leaking a raw third-party exception across the port
boundary — see `_run_with_bounded_retry`.
"""

from __future__ import annotations

import math
import time as _time
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from tfx_quant.application.ports.yahoo_history_query import YahooBar, YahooHistoryQueryError
from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.telemetry import get_logger, log_debug, log_info, log_warning

_logger = get_logger(__name__)

_REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")
_HISTORY_KWARGS: dict[str, Any] = {
    "interval": "1h",
    "auto_adjust": False,
    "actions": False,
    "prepost": False,
    "repair": False,
    "keepna": False,
    "raise_errors": True,
}
"""Every price/behavior-affecting `Ticker.history()` kwarg is pinned explicitly here —
never left to rely on `yfinance`'s own default (which for `auto_adjust` is `True`, the
opposite of what the implementation prompt requires)."""

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BASE_DELAY_SECONDS = 1.0
_DEFAULT_MAX_DELAY_SECONDS = 10.0
_DEFAULT_MULTIPLIER = 2.0
_DEFAULT_TIMEOUT_SECONDS = 10.0


class YfinanceHistoryQueryAdapter:
    """Implements `application.ports.yahoo_history_query.YahooHistoryQueryPort`
    against the real `yfinance` package."""

    def __init__(
        self,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS,
        max_delay_seconds: float = _DEFAULT_MAX_DELAY_SECONDS,
        multiplier: float = _DEFAULT_MULTIPLIER,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._multiplier = multiplier
        self._timeout_seconds = timeout_seconds

    def query_1h_bars(
        self, *, yahoo_ticker: str, start_date: date, end_date: date
    ) -> Sequence[YahooBar]:
        # yfinance's own `end` is exclusive-by-date (see this module's docstring) —
        # this port's contract is inclusive on both ends, so the translation happens
        # here, never leaked to the caller.
        end_exclusive = end_date + timedelta(days=1)
        log_info(
            _logger,
            "yahoo_history_query_requested",
            yahoo_ticker=yahoo_ticker,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            interval="1h",
        )
        start = _time.monotonic()
        bars = self._run_with_bounded_retry(yahoo_ticker, start_date, end_exclusive)
        log_info(
            _logger,
            "yahoo_history_query_result",
            yahoo_ticker=yahoo_ticker,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            record_count=len(bars),
            duration_ms=(_time.monotonic() - start) * 1000,
        )
        return bars

    def _run_with_bounded_retry(
        self, yahoo_ticker: str, start_date: date, end_exclusive: date
    ) -> list[YahooBar]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._fetch_and_normalize(yahoo_ticker, start_date, end_exclusive)
            except _RETRYABLE_EXCEPTIONS as exc:
                if attempt >= self._max_attempts:
                    log_warning(
                        _logger,
                        "yahoo_history_query_failed_after_retries",
                        yahoo_ticker=yahoo_ticker,
                        attempts=attempt,
                        error=str(exc),
                    )
                    raise YahooHistoryQueryError(
                        f"yfinance 查詢重試 {attempt} 次後仍失敗（{yahoo_ticker} "
                        f"{start_date}~{end_exclusive}）：{exc}"
                    ) from exc
                delay = min(
                    self._base_delay_seconds * (self._multiplier ** (attempt - 1)),
                    self._max_delay_seconds,
                )
                log_warning(
                    _logger,
                    "yahoo_history_query_attempt_failed",
                    yahoo_ticker=yahoo_ticker,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    retry_delay_seconds=delay,
                    error=str(exc),
                )
                _time.sleep(delay)
            except YahooHistoryQueryError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a raw yfinance/pandas type
                log_warning(
                    _logger,
                    "yahoo_history_query_non_retryable_error",
                    yahoo_ticker=yahoo_ticker,
                    error=str(exc),
                )
                raise YahooHistoryQueryError(
                    f"yfinance 查詢失敗，非可重試錯誤（{yahoo_ticker} "
                    f"{start_date}~{end_exclusive}）：{exc}"
                ) from exc

    def _fetch_and_normalize(
        self, yahoo_ticker: str, start_date: date, end_exclusive: date
    ) -> list[YahooBar]:
        import yfinance as yf  # isolated to this module only — see module docstring

        ticker = yf.Ticker(yahoo_ticker)
        df = ticker.history(
            start=start_date.isoformat(),
            end=end_exclusive.isoformat(),
            timeout=self._timeout_seconds,
            **_HISTORY_KWARGS,
        )
        return _normalize_dataframe(df, yahoo_ticker)


def _normalize_dataframe(df: Any, yahoo_ticker: str) -> list[YahooBar]:
    if df is None or df.empty:
        return []

    missing_columns = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing_columns:
        raise YahooHistoryQueryError(
            f"yfinance 回傳資料缺少必要欄位 {missing_columns}（ticker={yahoo_ticker}）— "
            "可能是套件/API 回傳格式變更，本次查詢視為失敗"
        )

    if df.index.tz is None:
        raise YahooHistoryQueryError(
            f"yfinance 回傳的時間索引不含時區資訊（ticker={yahoo_ticker}），無法安全解析"
        )

    sorted_df = df.sort_index()
    if not sorted_df.index.equals(df.index):
        log_debug(_logger, "yahoo_history_rows_reordered", yahoo_ticker=yahoo_ticker)

    seen_at: set[Any] = set()
    bars: list[YahooBar] = []
    dropped_duplicate = 0
    dropped_invalid = 0
    for index_value, row in sorted_df.iterrows():
        at_utc = index_value.tz_convert("UTC")
        if at_utc in seen_at:
            dropped_duplicate += 1
            continue
        seen_at.add(at_utc)

        parsed = _parse_row(row, at_utc, yahoo_ticker=yahoo_ticker)
        if parsed is None:
            dropped_invalid += 1
            continue
        bars.append(parsed)

    if dropped_duplicate or dropped_invalid:
        log_debug(
            _logger,
            "yahoo_history_rows_dropped",
            yahoo_ticker=yahoo_ticker,
            dropped_duplicate=dropped_duplicate,
            dropped_invalid=dropped_invalid,
            kept=len(bars),
        )
    return bars


def _parse_row(row: Any, at_utc: Any, *, yahoo_ticker: str = "") -> YahooBar | None:
    raw_values = {name: row[name] for name in _REQUIRED_COLUMNS}
    for value in raw_values.values():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None

    try:
        open_ = Decimal(str(raw_values["Open"]))
        high = Decimal(str(raw_values["High"]))
        low = Decimal(str(raw_values["Low"]))
        close = Decimal(str(raw_values["Close"]))
        volume = int(raw_values["Volume"])
    except (InvalidOperation, ValueError, TypeError):
        return None

    if open_ <= 0 or high <= 0 or low <= 0 or close <= 0 or volume < 0:
        return None

    at_taipei = at_utc.to_pydatetime().astimezone(TAIPEI_TZ)
    # Yahoo's TAIEX hourly index labels are clock-hour aligned (09:00, 10:00, ...),
    # while TAIFEX's canonical session begins at 08:45. The runtime explicitly uses
    # ^TWII as its yfinance-only TXF/MXF proxy, so shift those labels onto the futures
    # grid. No other Yahoo ticker is altered.
    if yahoo_ticker == "^TWII":
        at_taipei -= timedelta(minutes=15)
    return YahooBar(at=at_taipei, open=open_, high=high, low=low, close=close, volume=volume)


def _retryable_exception_types() -> tuple[type[Exception], ...]:
    """Best-effort, isolated-import mapping of transient failure types — see this
    module's docstring's honesty caveat about the unverified `curl_cffi`/`requests`
    exception hierarchy."""
    types: list[type[Exception]] = [ConnectionError, TimeoutError, OSError]
    try:
        from yfinance.exceptions import YFRateLimitError

        types.append(YFRateLimitError)
    except ImportError:
        pass
    try:
        import requests.exceptions as _requests_exceptions

        types.append(_requests_exceptions.RequestException)
    except ImportError:
        pass
    return tuple(types)


_RETRYABLE_EXCEPTIONS = _retryable_exception_types()
