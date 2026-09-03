"""Real Clock implementation."""

from __future__ import annotations

from tfx_quant.domain.timestamp import Timestamp


class SystemClock:
    """Implements `application.ports.clock.Clock` using wall-clock time."""

    def now(self) -> Timestamp:
        return Timestamp.now()
