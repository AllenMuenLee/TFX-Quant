"""HistoricalPriceQueryPort — the controlled seam for the vendor's official `GetKLine`
history query (Feature 04 extension: see `docs/adr/0007-two-month-bar-history-
persistence.md`'s extension decision).

Only `infrastructure.yuanta` may ever import vendor (`pythonnet`/`YuantaOneAPI`) types —
same boundary `application.ports.yuanta_gateways` documents. This port exists
specifically so `application.market_data.bar_history_backfill_service
.BarHistoryBackfillService` can ask for a range of already-closed 60-minute bars without
knowing anything about SPARK API's callback shape.

**Honesty caveat (do not remove or soften this without re-reading the live docs):** the
official `GetKLine` docs page attaches "註1：僅提供台股上市櫃商品查詢" (TWSE/OTC-listed
securities only) to its `MarketType` parameter, yet the same docs' own `enumMarketType`
reference does define a `TAIFEX`（期貨）member. This codebase calls `GetKLine` with
`MarketType=TAIFEX` anyway per an explicit, user-confirmed product decision — not because
the restriction note was found to be wrong. No implementation in this codebase has ever
been exercised against a real vendor login (no .NET 8 SDK/DLL available in this
environment — see `[[yuanta-spark-api-pivot]]`), so whether the vendor actually returns
futures data for this call, an empty result, or an outright rejection is unverified.
Every caller of this port MUST treat an empty result, a raised
`HistoricalPriceQueryError`, or any bar that fails to resolve against
`TradingCalendar.boundary_for_open` as "still a gap" — never retry-forever, never
fabricate, never present a guess as recorded history.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from tfx_quant.domain.account import TradingAccount


@dataclass(frozen=True, slots=True)
class VendorKLineBar:
    """One row of a `GetKLine` response, already parsed into typed primitives (see
    `infrastructure.yuanta.market_data_parsing.parse_kline_bar`) but not yet resolved
    into a domain `Bar` — that resolution needs `TradingCalendar.boundary_for_open` and
    an `InstrumentMasterEntry`, neither of which belongs in this vendor-shaped DTO."""

    at: datetime
    """Tz-aware Asia/Taipei. The vendor's own `TimeStamp` field — whether this labels
    the bar's open or close is not documented for intraday periods; see this module's
    docstring. Never treat this as pre-validated against any bar-boundary grid."""
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class HistoricalPriceQueryError(Exception):
    """Raised when a `GetKLine` call could not be completed — the vendor call itself
    was rejected, no response arrived within the adapter's timeout, or the vendor
    reported an error. Distinct from "the vendor genuinely has no bars for this range"
    (an empty `Sequence[VendorKLineBar]`, not an exception). Callers must treat this the
    same as an empty result: the requested range stays an unresolved gap, retried (if at
    all) only on a future trigger, never by looping synchronously here."""


class HistoricalPriceQueryPort(Protocol):
    def query_60m_kline(
        self,
        *,
        account: TradingAccount,
        vendor_symbol: str,
        start_date: date,
        end_date: date,
    ) -> Sequence[VendorKLineBar]:
        """已收盤 60 分 K 歷史查詢 (`GetKLine`, `KLineType`=六十分線, `MarketType`=
        `TAIFEX`). `[start_date, end_date]` is inclusive; callers are responsible for
        keeping the span within the vendor's own per-call limit (5 calendar days for
        60分k — see `domain.bar_history_backfill.chunk_consecutive_days`) and for
        pacing calls at the vendor's documented ≤1/sec rate for this function. Blocks
        the calling thread until the vendor's response arrives or the implementation's
        own timeout elapses — callers must never invoke this from a thread that must
        stay responsive (e.g. the `EventCoordinator` dispatch thread)."""
        ...
