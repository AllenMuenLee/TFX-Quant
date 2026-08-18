"""策略訊號 — a strategy-generated trading signal, not yet an order."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.instrument import Instrument
from tfx_quant.domain.timestamp import Timestamp


class SignalKind(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    ADD_LONG = "ADD_LONG"
    ADD_SHORT = "ADD_SHORT"
    EXIT_ALL = "EXIT_ALL"
    REVERSE = "REVERSE"


@dataclass(frozen=True, slots=True)
class StrategySignal:
    kind: SignalKind
    instrument: Instrument
    contract: ContractMonth
    at: Timestamp
    reason: str
