"""Bounded, automatically expiring workflow/order diagnostic elevation."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class DiagnosticStatus:
    workflow_id: str | None
    order_id: str | None
    remaining_events: int
    remaining_seconds: float


class DiagnosticMode:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._workflow_id: str | None = None
        self._order_id: str | None = None
        self._expires_at = 0.0
        self._remaining = 0

    def enable(
        self,
        *,
        workflow_id: str | None = None,
        order_id: str | None = None,
        duration: timedelta = timedelta(minutes=10),
        max_events: int = 1_000,
    ) -> None:
        if (workflow_id is None) == (order_id is None):
            raise ValueError("exactly one of workflow_id or order_id is required")
        if duration.total_seconds() <= 0 or max_events <= 0:
            raise ValueError("duration and max_events must be positive")
        with self._lock:
            self._workflow_id = workflow_id
            self._order_id = order_id
            self._expires_at = self._monotonic() + duration.total_seconds()
            self._remaining = max_events

    def disable(self) -> None:
        with self._lock:
            self._clear()

    def allows(self, *, workflow_id: str | None, order_id: str | None) -> bool:
        with self._lock:
            if self._remaining <= 0 or self._monotonic() >= self._expires_at:
                self._clear()
                return False
            matches = (self._workflow_id is not None and workflow_id == self._workflow_id) or (
                self._order_id is not None and order_id == self._order_id
            )
            if not matches:
                return False
            self._remaining -= 1
            return True

    def status(self) -> DiagnosticStatus | None:
        with self._lock:
            remaining_seconds = self._expires_at - self._monotonic()
            if self._remaining <= 0 or remaining_seconds <= 0:
                self._clear()
                return None
            return DiagnosticStatus(
                workflow_id=self._workflow_id,
                order_id=self._order_id,
                remaining_events=self._remaining,
                remaining_seconds=remaining_seconds,
            )

    def _clear(self) -> None:
        self._workflow_id = None
        self._order_id = None
        self._expires_at = 0.0
        self._remaining = 0


diagnostic_mode = DiagnosticMode()

__all__ = ["DiagnosticMode", "DiagnosticStatus", "diagnostic_mode"]
