from __future__ import annotations

import importlib.util
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from tfx_quant.infrastructure.yuanta.quote_com_host import default_quote_api_directory

_OCX_NAME = "YuantaQuote_v2.1.2.9.ocx"


@dataclass(frozen=True, slots=True)
class QuotePreflightResult:
    passed: bool
    errors: tuple[str, ...]


def check_quote_runtime(api_directory: Path | None = None) -> QuotePreflightResult:
    """Verify the environment the quote OCX needs.

    ``使用說明.txt`` names Python 3.9 / comtypes 1.1.11 as the versions the vendor's
    sample was written against; the pinned requirement that actually matters is the
    32-bit interpreter, since the OCX is 32-bit.  A live quote session has been
    confirmed on 32-bit Python 3.11 with comtypes 1.4.16, so the patch versions are
    not asserted here.
    """
    directory = api_directory or default_quote_api_directory()
    errors: list[str] = []
    if sys.platform != "win32":
        errors.append("Yuanta quote OCX requires Windows")
    if struct.calcsize("P") * 8 != 32:
        errors.append("The Yuanta quote OCX is 32-bit; run the 32-bit interpreter")
    if importlib.util.find_spec("comtypes") is None:
        errors.append("Install the comtypes dependency")
    if importlib.util.find_spec("wx") is None:
        errors.append("Install wxPython; the quote control needs a window to be hosted in")
    ocx = directory / _OCX_NAME
    if not ocx.is_file():
        errors.append(
            f"Missing {ocx}; copy the QAPI folder and run install_ytocx.bat as administrator"
        )
    return QuotePreflightResult(not errors, tuple(errors))
