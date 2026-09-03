"""Capped, cancellable backoff for login/initialization retries.

Per the implementation prompt: "登入或初始化失敗時不得重複狂試；採有上限退避並允許取消"
(on login/init failure, don't hammer retries — use a capped backoff and allow
cancellation). Pure and thread-agnostic — no sleeping happens inside this class itself
so it's trivial to unit test; the caller (`BrokerSessionOrchestrator`) is responsible
for actually waiting `next_delay_seconds()` and checking `is_cancelled`/`is_exhausted`
around that wait.
"""

from __future__ import annotations

import threading

from tfx_quant.telemetry import get_logger, log_info

_logger = get_logger(__name__)


class BackoffPolicy:
    """Exponential backoff with a hard attempt cap and a cancellation token.

    `base_delay_seconds * (multiplier ** attempt)`, clamped to `max_delay_seconds`.
    After `max_attempts` failed attempts, `is_exhausted` becomes True and
    `next_delay_seconds()` raises — the caller must stop retrying and surface a
    terminal failure requiring manual intervention.
    """

    def __init__(
        self,
        *,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
        multiplier: float = 2.0,
        max_attempts: int = 5,
    ) -> None:
        if base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if multiplier <= 1:
            raise ValueError("multiplier must be > 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._multiplier = multiplier
        self._max_attempts = max_attempts
        self._attempt = 0
        self._cancel_event = threading.Event()

    @property
    def attempt_count(self) -> int:
        return self._attempt

    @property
    def is_exhausted(self) -> bool:
        return self._attempt >= self._max_attempts

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def record_failure(self) -> None:
        """Call once per failed attempt, before requesting the next delay."""
        self._attempt += 1
        log_info(_logger, "backoff_attempt_recorded", attempt=self._attempt)

    def next_delay_seconds(self) -> float:
        """The delay to wait before the next attempt.

        Raises `RuntimeError` if `is_exhausted` — check that first.
        """
        if self.is_exhausted:
            raise RuntimeError(
                f"backoff exhausted after {self._attempt} attempts "
                f"(max_attempts={self._max_attempts})"
            )
        delay = self._base_delay_seconds * (self._multiplier ** (self._attempt - 1))
        delay = min(delay, self._max_delay_seconds)
        # No jitter: this policy is purely deterministic (see class docstring's exact
        # formula) — a caller logging the implementation prompt's "jitter" field
        # accurately reports 0.
        log_info(_logger, "backoff_delay_computed", attempt=self._attempt, delay_seconds=delay)
        return delay

    def cancel(self) -> None:
        self._cancel_event.set()
        log_info(_logger, "backoff_cancelled", attempt=self._attempt)

    def reset(self) -> None:
        """Call after a successful attempt to reset attempt count for future use."""
        self._attempt = 0
        self._cancel_event.clear()
