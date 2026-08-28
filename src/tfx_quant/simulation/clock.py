from __future__ import annotations

from datetime import timedelta

from tfx_quant.domain.timestamp import Timestamp


class VirtualClock:
    """A deterministic, monotonic implementation of the application clock port."""

    def __init__(self, initial: Timestamp) -> None:
        self._now = initial

    def now(self) -> Timestamp:
        return self._now

    def advance_to(self, target: Timestamp) -> None:
        if target.value < self._now.value:
            raise ValueError("virtual clock cannot move backwards")
        self._now = target

    def advance(self, delta: timedelta) -> None:
        if delta.total_seconds() < 0:
            raise ValueError("virtual clock cannot move backwards")
        self._now = Timestamp(self._now.value + delta)
