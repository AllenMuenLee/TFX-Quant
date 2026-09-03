from __future__ import annotations

from typing import Protocol

from tfx_quant.application.events.events import Event


class EventPublisher(Protocol):
    def publish(self, event: Event) -> None: ...
