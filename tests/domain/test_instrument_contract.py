from __future__ import annotations

import pytest

from tfx_quant.domain.contract import ContractMonth
from tfx_quant.domain.errors import InvalidContractMonthError
from tfx_quant.domain.instrument import Instrument


def test_instrument_only_has_mxf_and_txf() -> None:
    assert {i.value for i in Instrument} == {"MXF", "TXF"}


def test_instrument_rejects_unknown_code() -> None:
    with pytest.raises(ValueError):
        Instrument("NOPE")


def test_contract_month_accepts_valid_month() -> None:
    contract = ContractMonth(year=2026, month=9)
    assert contract.code == "202609"


@pytest.mark.parametrize("month", [0, 13, -1])
def test_contract_month_rejects_invalid_month(month: int) -> None:
    with pytest.raises(InvalidContractMonthError):
        ContractMonth(year=2026, month=month)


def test_contract_month_from_code_round_trips() -> None:
    contract = ContractMonth.from_code("202609")
    assert contract == ContractMonth(year=2026, month=9)


@pytest.mark.parametrize("code", ["", "2026", "2026-09", "20269a"])
def test_contract_month_from_code_rejects_malformed(code: str) -> None:
    with pytest.raises(InvalidContractMonthError):
        ContractMonth.from_code(code)
