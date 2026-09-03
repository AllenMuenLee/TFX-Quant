"""Generic `BarSignalStateStore` implementations — no Yuanta/COM dependency, same
category as `infrastructure/clock.py`/`infrastructure/identity.py`.

Feature 04/05 (market data bars, strategy signal engine) don't exist yet, so there is
no real bar/signal state to clear — `NullBarSignalStateStore` is the composition-root
default until then; `InMemoryBarSignalStateStore` exists for tests that want to assert
`InstrumentSelectionService.switch_to()` actually called `clear()`.
"""

from __future__ import annotations

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument


class NullBarSignalStateStore:
    """Implements `application.ports.bar_signal_state.BarSignalStateStore` as a no-op."""

    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        pass


class InMemoryBarSignalStateStore:
    """Implements `BarSignalStateStore`, recording every `clear()` call for assertions."""

    def __init__(self) -> None:
        self.clear_calls: list[tuple[Instrument, ContractMonth]] = []

    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        self.clear_calls.append((instrument, contract))
