"""BarSignalStateStore — the K棒／訊號 state-reset hook instrument switching depends on.

Feature 04 (market data/60-minute bars) and Feature 05 (strategy signal engine) own the
real bar/signal state; neither exists yet as of Feature 03. This Protocol is the seam
`application.instrument_selection.InstrumentSelectionService.switch_to()` calls
`clear()` on so the reset actually takes effect once those features exist, without
Feature 03 needing to know their internals.
`infrastructure.bar_signal_state.NullBarSignalStateStore` is a safe placeholder default
until then.
"""

from __future__ import annotations

from typing import Protocol

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument


class BarSignalStateStore(Protocol):
    def clear(self, instrument: Instrument, contract: ContractMonth) -> None:
        """Discard any in-memory K-bar/signal state left over from the previous
        instrument/contract, so the strategy starts fresh against the new one."""
        ...
