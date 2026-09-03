"""ResolvedSelection — an instrument/contract pair that has passed master-file
validation and is ready to be shown to the operator for confirmation (or applied by
`InstrumentSelectionService.switch_to()`)."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.instrument_master import InstrumentMasterEntry


@dataclass(frozen=True, slots=True)
class ResolvedSelection:
    """The result of resolving either a manual pick or AUTO near-month selection
    against the controlled instrument master — not yet applied/subscribed."""

    instrument: Instrument
    contract: ContractMonth
    entry: InstrumentMasterEntry

    def __post_init__(self) -> None:
        if (self.instrument, self.contract) != (self.entry.instrument, self.entry.contract):
            raise ValueError(
                "ResolvedSelection.instrument/contract must match entry.instrument/contract"
            )
