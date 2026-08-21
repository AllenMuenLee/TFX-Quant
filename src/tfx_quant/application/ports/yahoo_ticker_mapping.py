"""YahooTickerMappingRepository — the controlled 內部商品／契約 -> Yahoo ticker lookup
port (Feature 04 extension: see `docs/adr/0007-two-month-bar-history-persistence.md`'s
yfinance extension decision).

Mirrors `InstrumentMasterRepository`/`TradingCalendarRepository`'s "controlled data, not
a formula" pattern (ADR 0005): unlike `domain.instrument_master.futures_quote_symbol()`
(a documented, computable Yuanta encoding), no public spec defines how a TAIFEX futures
contract month maps to a Yahoo Finance ticker — Yahoo Finance's own futures-contract
ticker coverage and naming are not documented anywhere this codebase can cite, and
whether Yahoo publishes *any* usable series for a specific TAIFEX contract month at all
is genuinely unverified (see `infrastructure.market_data.
yahoo_ticker_mapping.example.json`'s own warning). This is therefore backed by a
version-controlled, operator-maintained JSON file, never guessed, never computed, and
never silently substituted with a continuous-contract alias unless an operator has
explicitly configured one as its own distinct entry (see the implementation prompt's
"若產品明確允許 continuous contract，必須以不同 instrument identity 保存並在 UI 標示").
"""

from __future__ import annotations

from typing import Protocol

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument


class YahooTickerMappingRepository(Protocol):
    def get(self, instrument: Instrument, contract: ContractMonth) -> str | None:
        """`None` means no confirmed Yahoo ticker mapping exists for this exact
        (instrument, contract) pair — callers MUST treat this as "backfill cannot run
        for this contract; leave every bar as a gap", never guess a ticker, never fall
        back to a different contract month's ticker or an unconfigured continuous-
        contract alias."""
        ...
