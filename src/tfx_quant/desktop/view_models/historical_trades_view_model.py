"""Keep counterfactual signals separate from executed trades and actual P&L."""

from collections.abc import Sequence
from dataclasses import replace

from tfx_quant.application.strategy_signal.history_replay import HistoricalTrade
from tfx_quant.domain.account import TradingAccount
from tfx_quant.domain.order_state_machine import OrderIntent


def missed_trades(
    candidates: Sequence[HistoricalTrade],
    orders: Sequence[OrderIntent],
    account: TradingAccount | None,
) -> list[HistoricalTrade]:
    results = []
    for candidate in candidates:
        executed = sum(
            order.filled_quantity
            for order in orders
            if account is not None
            and order.account == account
            and order.instrument == candidate.record.bar.instrument
            and order.contract == candidate.record.bar.contract
            and order.side == candidate.side
            and order.idempotency_key == candidate.decision.intent_key
        )
        remaining = candidate.quantity - executed
        if remaining > 0:
            results.append(replace(candidate, quantity=remaining))
    return results
