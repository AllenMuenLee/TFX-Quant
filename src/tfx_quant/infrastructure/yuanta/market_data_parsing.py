"""Pure parsing of SPARK API `StockTickResult` pushes into typed values.

Kept separate from `session_orchestrator.py` so that module doesn't grow unrelated
parsing responsibilities.

This module previously parsed the legacy OCX API's `OnGetMktAll` BSTR fields, which had
two undocumented-format gaps (`MatchTime`'s digit count, no per-trade sequence number —
`TolMatchQty` stood in as a proxy). SPARK API's `SubscribeStockTick`/`StockTickResult`
(交易/行情 docs, 分時明細訂閱 page) resolves both: `Time` is a structured `TYuantaTime`
object (`bytHour`/`bytMin`/`bytSec`/`ushtMSec`, not a raw digit string to parse), and
`SerialNo` is a real, documented per-symbol trade sequence number ("以股票代碼個別編序
號，從1開始，-1代表商品清盤") — no more `TolMatchQty`-as-proxy. See
`docs/adr/0006-market-data-and-bar-aggregation.md`'s addendum.

`SerialNo == -1` is the docs' documented 商品清盤 (end-of-session settlement) sentinel —
`parse_stock_tick_push` returns `None` for that case (nothing to feed the aggregator,
not a parse error), mirroring the old `TolMatchQty == -1` pre-market sentinel handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from tfx_quant.domain.timestamp import TAIPEI_TZ
from tfx_quant.infrastructure.yuanta.errors import MarketDataParseError

_SETTLEMENT_SENTINEL = -1


@dataclass(frozen=True, slots=True)
class ParsedStockTickPush:
    vendor_symbol: str
    price: Decimal
    size: int
    serial_no: int
    exchange_time: time


def _parse_yuanta_time(*, hour: int, minute: int, second: int, millisecond: int) -> time:
    try:
        return time(hour, minute, second, millisecond * 1000)
    except ValueError as exc:
        raise MarketDataParseError(
            f"StockTickResult.Time 數值超出範圍：{hour:02d}:{minute:02d}:{second:02d}."
            f"{millisecond:03d}"
        ) from exc


def _parse_decimal(raw: object, *, label: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise MarketDataParseError(f"{label} 不是有效數字：{raw!r}") from exc


def _parse_int(raw: object, *, label: str) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError) as exc:
        raise MarketDataParseError(f"{label} 不是有效整數：{raw!r}") from exc


def parse_stock_tick_push(
    *,
    stk_code: str,
    serial_no: object,
    deal_price: object,
    deal_vol: object,
    hour: object,
    minute: object,
    second: object,
    millisecond: object,
) -> ParsedStockTickPush | None:
    """Returns `None` for a 商品清盤 (settlement) push (`SerialNo == -1`) — no real
    trade to feed the aggregator. Raises `MarketDataParseError` for anything malformed.

    Callers pass `StockTickResult`'s fields individually (not the raw .NET object)
    so this module stays pure-Python and independently testable, matching this
    codebase's existing split between vendor-callback glue and parsing logic.
    """
    if not stk_code.strip():
        raise MarketDataParseError("StockTickResult 的 StkCode 為空白")

    serial_no_parsed = _parse_int(serial_no, label="SerialNo")
    if serial_no_parsed == _SETTLEMENT_SENTINEL:
        return None
    if serial_no_parsed < 1:
        raise MarketDataParseError(f"SerialNo 必須 >= 1（或 -1 代表清盤），得到 {serial_no_parsed}")

    price = _parse_decimal(deal_price, label="DealPrice")
    if price <= 0:
        raise MarketDataParseError(f"DealPrice 必須為正數，得到 {deal_price!r}")
    size = _parse_int(deal_vol, label="DealVol")
    if size < 0:
        raise MarketDataParseError(f"DealVol 必須 >= 0，得到 {size}")

    exchange_time = _parse_yuanta_time(
        hour=_parse_int(hour, label="Time.bytHour"),
        minute=_parse_int(minute, label="Time.bytMin"),
        second=_parse_int(second, label="Time.bytSec"),
        millisecond=_parse_int(millisecond, label="Time.ushtMSec"),
    )

    return ParsedStockTickPush(
        vendor_symbol=stk_code.strip(),
        price=price,
        size=size,
        serial_no=serial_no_parsed,
        exchange_time=exchange_time,
    )


@dataclass(frozen=True, slots=True)
class ParsedKLineBar:
    """One `GetKLine` result row, parsed into typed primitives — see
    `application.ports.historical_price_query.VendorKLineBar`, which this maps onto
    directly (`spark_historical_price_adapter.py` does that final, trivial conversion)."""

    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def parse_kline_bar(
    *,
    year: object,
    month: object,
    day: object,
    hour: object,
    minute: object,
    second: object,
    open_price: object,
    high_price: object,
    low_price: object,
    close_price: object,
    deal_vol: object,
) -> ParsedKLineBar:
    """Parses one `KLine` result row. Callers pass `TimeStamp`'s individual date/time
    components (not the raw `.NET System.DateTime` object) so this module stays
    pure-Python — same split as `parse_stock_tick_push`. Raises `MarketDataParseError`
    for anything malformed; callers must treat that as an unresolvable single row
    (skip it, don't abort the rest of the batch), matching this codebase's "never
    fabricate, only ever drop into an unresolved gap" policy for vendor data it can't
    trust."""
    try:
        at = datetime(
            _parse_int(year, label="TimeStamp.Year"),
            _parse_int(month, label="TimeStamp.Month"),
            _parse_int(day, label="TimeStamp.Day"),
            _parse_int(hour, label="TimeStamp.Hour"),
            _parse_int(minute, label="TimeStamp.Minute"),
            _parse_int(second, label="TimeStamp.Second"),
            tzinfo=TAIPEI_TZ,
        )
    except ValueError as exc:
        raise MarketDataParseError(f"KLine.TimeStamp 數值超出範圍：{exc}") from exc

    open_ = _parse_decimal(open_price, label="OpenPrice")
    high = _parse_decimal(high_price, label="HighPrice")
    low = _parse_decimal(low_price, label="LowPrice")
    close = _parse_decimal(close_price, label="ClosePrice")
    if high < low:
        raise MarketDataParseError(f"HighPrice ({high}) 小於 LowPrice ({low})")
    volume = _parse_int(deal_vol, label="DealVol")
    if volume < 0:
        raise MarketDataParseError(f"DealVol 必須 >= 0，得到 {volume}")

    return ParsedKLineBar(at=at, open=open_, high=high, low=low, close=close, volume=volume)
