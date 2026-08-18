"""Pure parsing of `OnGetMktAll` raw BSTR fields into typed values.

Kept separate from `session_orchestrator.py` so that module doesn't grow unrelated
parsing responsibilities — it already has one raw-string parser (`_parse_futures_accounts`
for `OnLogonS`'s `AccList`) as precedent for this split.

Two undocumented-format gaps, both flagged rather than guessed (see
`docs/adr/0006-market-data-and-bar-aggregation.md`):

- **`MatchTime`'s digit count is never stated** in `元大行情API.pdf` (just "char*
  MatchTime 成交時間"). Parsed defensively: 6 digits -> HHMMSS, 9 digits -> HHMMSS plus
  3-digit milliseconds. Any other length is rejected as malformed.
- **No documented per-trade sequence number** — `TolMatchQty` (total cumulative volume)
  is the closest available strictly-increasing-per-symbol field and is carried through
  as `cumulative_volume`, the ordering/dedup key `domain.bar_aggregator.BarAggregator`
  uses. See `domain/tick.py`.

`TolMatchQty == -1` is the PDF's documented 盤前行情資料 (pre-market data) sentinel —
`parse_market_data_push` returns `None` for that case (nothing to feed the aggregator,
not a parse error).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation

from tfx_quant.infrastructure.yuanta.errors import MarketDataParseError

_PRE_MARKET_SENTINEL = "-1"


@dataclass(frozen=True, slots=True)
class ParsedMarketDataPush:
    vendor_symbol: str
    price: Decimal
    size: int
    cumulative_volume: int
    exchange_time: time


def _parse_match_time(raw: str) -> time:
    digits = raw.strip()
    if len(digits) == 6 and digits.isdigit():
        hour, minute, second = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
        microsecond = 0
    elif len(digits) == 9 and digits.isdigit():
        hour, minute, second = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
        microsecond = int(digits[6:9]) * 1000
    else:
        raise MarketDataParseError(f"MatchTime 格式無法辨識（需為 6 或 9 位數字）：{raw!r}")
    try:
        return time(hour, minute, second, microsecond)
    except ValueError as exc:
        raise MarketDataParseError(f"MatchTime 數值超出範圍：{raw!r}") from exc


def _parse_decimal(raw: str, *, label: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except InvalidOperation as exc:
        raise MarketDataParseError(f"{label} 不是有效數字：{raw!r}") from exc


def _parse_int(raw: str, *, label: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise MarketDataParseError(f"{label} 不是有效整數：{raw!r}") from exc


def parse_market_data_push(
    *,
    symbol: str,
    match_time: str,
    match_pri: str,
    match_qty: str,
    tol_match_qty: str,
) -> ParsedMarketDataPush | None:
    """Returns `None` for a pre-market snapshot push (`TolMatchQty == -1`) — no real
    trade to feed the aggregator. Raises `MarketDataParseError` for anything malformed.
    """
    if not symbol.strip():
        raise MarketDataParseError("OnGetMktAll 的 Symbol 為空白")

    if tol_match_qty.strip() == _PRE_MARKET_SENTINEL:
        return None

    price = _parse_decimal(match_pri, label="MatchPri")
    if price <= 0:
        raise MarketDataParseError(f"MatchPri 必須為正數，得到 {match_pri!r}")
    size = _parse_int(match_qty, label="MatchQty")
    cumulative_volume = _parse_int(tol_match_qty, label="TolMatchQty")
    exchange_time = _parse_match_time(match_time)

    return ParsedMarketDataPush(
        vendor_symbol=symbol.strip(),
        price=price,
        size=size,
        cumulative_volume=cumulative_volume,
        exchange_time=exchange_time,
    )
