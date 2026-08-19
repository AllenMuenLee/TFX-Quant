"""Thin `TradeGatewayPort`/`QuoteGatewayPort` views over a real `IBrokerSession`.

Feature 01's narrow ports (`is_logged_in`, `query_open_orders`, `query_positions`,
`is_market_data_valid`, `subscribe`) predate `IBrokerSession` and stay as the surface
other features query. For the mock branch, `MockBrokerSession` doesn't need this —
`MockTradeGateway`/`MockQuoteGateway` are used directly (see `composition.py`).

`query_open_orders`/`query_positions` still raise rather than fabricating an "always
empty" answer: SPARK API's `GetRealReport`/`GetFutStoreSummary` results aren't parsed
into typed `Order`/`Position` objects yet (see `session_orchestrator.py`'s docstrings
on `handle_real_report_query_result`/`handle_position_query_result` — building typed
objects needs an idempotency-key mapping that's Feature 06/08's job). Returning `()`
here instead of raising would silently look like "confirmed no open orders" to
`StartupSafetyGate`'s "no unknown orders" check — a false safety signal — so these
still raise.

`subscribe`/`unsubscribe` are Feature 03's job and are implemented for real here: the
instrument/contract → vendor-symbol translation comes from the controlled
`InstrumentMasterRepository` (see `instrument_master_repository.py`), and the actual
`SubscribeStockTick`/`UnSubscribeStockTick` call goes through
`IBrokerSession.subscribe_market_data`/`unsubscribe_market_data`.
"""

from __future__ import annotations

from collections.abc import Sequence

from tfx_quant.application.instrument_selection.errors import InstrumentMasterEntryNotFoundError
from tfx_quant.application.ports.broker_session import IBrokerSession
from tfx_quant.application.ports.instrument_master import InstrumentMasterRepository
from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.order import Order
from tfx_quant.domain.position import Position

_NOT_YET_PARSED_MESSAGE = (
    "尚未實作將元大查詢結果解析為型別化物件（見 "
    "infrastructure/yuanta/broker_session_gateway_views.py）；"
    "委託/成交/持倉的結構化查詢預計於後續 feature 完成。"
)


class BrokerSessionTradeGatewayView:
    """Implements `TradeGatewayPort` — delegates the boolean to the real session."""

    def __init__(self, broker_session: IBrokerSession) -> None:
        self._broker_session = broker_session

    def is_logged_in(self) -> bool:
        return self._broker_session.capabilities.login

    def query_open_orders(self) -> Sequence[Order]:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)

    def query_positions(self) -> Sequence[Position]:
        raise NotImplementedError(_NOT_YET_PARSED_MESSAGE)


class BrokerSessionQuoteGatewayView:
    """Implements `QuoteGatewayPort` — delegates the boolean to the real session and
    translates instrument/contract to a vendor symbol via the controlled master file."""

    def __init__(
        self, broker_session: IBrokerSession, instrument_master: InstrumentMasterRepository
    ) -> None:
        self._broker_session = broker_session
        self._instrument_master = instrument_master

    def is_market_data_valid(self) -> bool:
        return self._broker_session.capabilities.market_data

    def subscribe(self, instrument: Instrument, contract: ContractMonth) -> None:
        entry = self._instrument_master.get(instrument, contract)
        if entry is None:
            raise InstrumentMasterEntryNotFoundError(
                f"商品主檔缺漏：{instrument.value} {contract.code}，無法訂閱行情"
            )
        self._broker_session.subscribe_market_data(entry.vendor_symbol)

    def unsubscribe(self, instrument: Instrument, contract: ContractMonth) -> None:
        entry = self._instrument_master.get(instrument, contract)
        if entry is None:
            # Nothing to translate to — if it was never subscribable, it was never
            # subscribed either, so there's nothing to clean up.
            return
        self._broker_session.unsubscribe_market_data(entry.vendor_symbol)
