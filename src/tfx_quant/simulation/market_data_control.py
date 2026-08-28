from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from pydantic import SecretStr

from tfx_quant.application.ports.quote_gateway import QuoteGateway
from tfx_quant.desktop.quote_runtime import QuoteRuntime
from tfx_quant.domain.market_data import MarketDataGap, RawMarketEvent
from tfx_quant.simulation.quote_gateway import SimulatedQuoteGateway
from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)


class SimulationDataSource(StrEnum):
    MOCK = "MOCK"
    REAL_YUANTA_QUOTES = "REAL_YUANTA_QUOTES"


class SimulationMarketDataController:
    """Selects quote data independently while trade execution stays simulated."""

    def __init__(self) -> None:
        self._source = SimulationDataSource.MOCK
        self._runtime: QuoteRuntime | None = None

    @property
    def source(self) -> str:
        return self._source.value

    def attach(self, runtime: QuoteRuntime) -> None:
        self._runtime = runtime

    def gateway_factory(
        self,
        on_event: Callable[[RawMarketEvent], None],
        on_gap: Callable[[MarketDataGap], None],
    ) -> QuoteGateway:
        if self._source is SimulationDataSource.MOCK:
            return SimulatedQuoteGateway(on_event, on_gap)
        # This is quote-only.  No trading adapter is constructed by this controller.
        from tfx_quant.infrastructure.yuanta.quote_com_host import YuantaQuoteComHost

        return YuantaQuoteComHost(on_event, on_gap)

    def use_mock_data(self) -> None:
        runtime = self._require_runtime()
        runtime.stop()
        self._source = SimulationDataSource.MOCK
        runtime.start("SIMULATION", SecretStr("SIMULATION-ONLY"))
        log_info(_logger, "simulation_market_data_source_changed", source=self.source)

    def use_real_data(self, user_id: str, password: str) -> None:
        user_id = user_id.strip()
        if not user_id or not password:
            raise ValueError("Yuanta quote user ID and password are required")
        runtime = self._require_runtime()
        runtime.stop()
        self._source = SimulationDataSource.REAL_YUANTA_QUOTES
        try:
            runtime.start(user_id, SecretStr(password))
        except Exception:
            # Fail closed and disconnected. Never silently substitute mock quotes.
            runtime.stop()
            raise
        log_info(
            _logger,
            "simulation_market_data_source_changed",
            source=self.source,
            trade_execution="OFFLINE_SIMULATOR",
        )

    def _require_runtime(self) -> QuoteRuntime:
        if self._runtime is None:
            raise RuntimeError("simulation market-data controller is not attached")
        return self._runtime
