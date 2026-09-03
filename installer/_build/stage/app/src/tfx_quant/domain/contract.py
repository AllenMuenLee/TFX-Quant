"""契約月份 — a single futures contract month, e.g. 2026-09."""

from __future__ import annotations

from dataclasses import dataclass

from tfx_quant.domain.errors import InvalidContractMonthError

_MIN_YEAR = 2000
_MAX_YEAR = 2100


@dataclass(frozen=True, slots=True)
class ContractMonth:
    """An immutable (year, month) pair identifying one futures contract month."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not (_MIN_YEAR <= self.year <= _MAX_YEAR):
            raise InvalidContractMonthError(
                f"year must be between {_MIN_YEAR} and {_MAX_YEAR}, got {self.year}"
            )
        if not (1 <= self.month <= 12):
            raise InvalidContractMonthError(f"month must be 1-12, got {self.month}")

    @property
    def code(self) -> str:
        """The vendor-style YYYYMM contract code, e.g. "202609"."""
        return f"{self.year:04d}{self.month:02d}"

    @classmethod
    def from_code(cls, code: str) -> ContractMonth:
        """Parse a YYYYMM code (e.g. "202609") into a ContractMonth."""
        if len(code) != 6 or not code.isdigit():
            raise InvalidContractMonthError(
                f"contract code must be 6 digits (YYYYMM), got {code!r}"
            )
        return cls(year=int(code[:4]), month=int(code[4:6]))
