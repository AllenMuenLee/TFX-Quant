"""Clock port — replaceable time source so tests never depend on wall-clock time."""

from __future__ import annotations

from typing import Protocol

from tfx_quant.domain.timestamp import Timestamp


class Clock(Protocol):
    def now(self) -> Timestamp: ...
