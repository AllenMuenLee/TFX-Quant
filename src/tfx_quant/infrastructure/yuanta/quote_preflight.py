from __future__ import annotations

import importlib.util
import struct
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class QuotePreflightResult:
    passed: bool
    errors: tuple[str, ...]


def check_quote_runtime(api_directory: Path = Path(r"C:\Yuanta\QAPI")) -> QuotePreflightResult:
    errors: list[str] = []
    if sys.platform != "win32":
        errors.append("Yuanta quote OCX requires Windows")
    if struct.calcsize("P") * 8 != 32:
        errors.append("Yuanta quote documentation requires 32-bit Python")
    if sys.version_info[:2] != (3, 9):
        errors.append("Yuanta quote example runtime requires Python 3.9")
    if importlib.util.find_spec("comtypes") is None:
        errors.append("Install the documented comtypes 1.1.11 dependency")
    ocx = api_directory / "YuantaQuote_v2.1.2.9.ocx"
    if not ocx.is_file():
        errors.append(
            f"Missing {ocx}; copy the QAPI folder and run install_ytocx.bat as administrator"
        )
    return QuotePreflightResult(not errors, tuple(errors))
