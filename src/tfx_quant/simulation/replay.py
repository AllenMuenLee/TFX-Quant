from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from tfx_quant.domain.market_data import RawMarketEvent
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.simulation.clock import VirtualClock


class ReplaySource(StrEnum):
    TEST_FIXTURE = "test_fixture"
    LOCAL_RECORDER = "local_recorder"


@dataclass(frozen=True, slots=True)
class ReplayMetadata:
    scenario_id: str
    fixture_version: str
    random_seed: int
    source: ReplaySource

    def __post_init__(self) -> None:
        if not self.scenario_id.strip() or not self.fixture_version.strip():
            raise ValueError("scenario_id and fixture_version are required")


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    at: Timestamp
    event: RawMarketEvent


class ReplayHarness:
    """Ordered replay that never skips queued events when the clock jumps."""

    def __init__(
        self,
        metadata: ReplayMetadata,
        clock: VirtualClock,
        events: list[ReplayEvent],
    ) -> None:
        self.metadata, self.clock = metadata, clock
        self._events = sorted(events, key=lambda item: (item.at.value, item.event.sequence))
        self._index = 0
        self.speed = 1.0
        self.paused = True

    @property
    def pending_count(self) -> int:
        return len(self._events) - self._index

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("replay speed must be positive")
        self.speed = speed

    def play(self) -> None:
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def jump_to(self, target: Timestamp, consume: Callable[[RawMarketEvent], None]) -> int:
        if target.value < self.clock.now().value:
            raise ValueError("replay cannot jump backwards")
        delivered = 0
        while (
            self._index < len(self._events)
            and self._events[self._index].at.value <= target.value
        ):
            item = self._events[self._index]
            self.clock.advance_to(item.at)
            consume(item.event)
            self._index += 1
            delivered += 1
        self.clock.advance_to(target)
        return delivered
