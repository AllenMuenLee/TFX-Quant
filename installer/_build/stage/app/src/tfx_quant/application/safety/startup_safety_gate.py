"""StartupSafetyGate — the only mechanism that may allow Starting -> Running.

Per the global safety rule: the strategy may enter `Running` only when the API is
logged in, the account is confirmed, market data is valid, an order query has
completed, positions are synced, there are no unknown orders, the selected
instrument/contract has been explicitly confirmed, the quote/position/order
instrument-contract identities all agree, and the user has explicitly pressed start.
All nine, every time — no partial bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class SafetyChecklist:
    api_logged_in: bool = False
    account_confirmed: bool = False
    market_data_valid: bool = False
    order_query_completed: bool = False
    position_synced: bool = False
    no_unknown_orders: bool = False
    instrument_contract_confirmed: bool = False
    """True once the operator has explicitly confirmed the selected instrument and
    contract month — manual selection, or the auto-resolved near-month contract (see
    `application.instrument_selection`) — never inferred automatically. Feature 03's
    acceptance criteria: 若支援近月自動選擇，必須顯示解析後契約並要求啟動前確認。"""
    quote_position_order_consistent: bool = False
    """True only when the subscribed quote and every known position/open order all
    identify the same instrument/contract as the current selection — see
    `application.instrument_selection.validation.check_quote_position_order_consistent`.
    Feature 03's acceptance criteria: 行情、持倉、委託三者不一致時不得啟動。"""
    user_pressed_start: bool = False
    """Only ever set True by a UI button-click handler — never automatically."""

    def unmet_items(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self) if not getattr(self, f.name))

    @property
    def is_fully_satisfied(self) -> bool:
        return not self.unmet_items()


class StartupSafetyGate:
    @staticmethod
    def can_enter_running(checklist: SafetyChecklist) -> bool:
        return checklist.is_fully_satisfied
