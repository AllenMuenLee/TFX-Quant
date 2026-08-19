"""InstrumentMasterRepository — the controlled 商品主檔 lookup port.

`infrastructure.yuanta.instrument_master_repository.JsonInstrumentMasterRepository` is
the real (and only, mock included — the data is genuinely controlled either way, not
something worth faking differently in tests) implementation, backed by a
version-controlled-by-ops JSON file rather than a live vendor query: neither extracted
vendor PDF documents a "list tradable contracts" call, so this is not a gap Feature 03
could close by calling the real API — see `docs/adr/0005-instrument-master-and-
selection.md`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry


class InstrumentMasterRepository(Protocol):
    def get(self, instrument: Instrument, contract: ContractMonth) -> InstrumentMasterEntry | None:
        """None means "主檔缺漏" — no controlled data exists for this pair."""
        ...

    def list_for(self, instrument: Instrument) -> Sequence[InstrumentMasterEntry]:
        """Every known contract month for `instrument`, tradable or not — callers
        filter on `.tradable`/`.expiry_date` themselves (see
        `application.instrument_selection`)."""
        ...
