"""ReconnectBackoffPolicy — capped exponential backoff with jitter for post-ready
reconnect attempts.

Distinct from `infrastructure.yuanta.backoff.BackoffPolicy` (deliberately jitter-free —
see that module's own docstring — since it only ever retries a single in-progress login
attempt in isolation): a reconnect episode retries `IBrokerSession.start()` again after
the *whole session* has already gone from ready to unusable, and per this feature's
implementation prompt ("重連採有上限的 exponential backoff 加 jitter；避免登入風暴")
jitter here specifically exists to desynchronize many independently-running instances of
this software from all retrying in lockstep after e.g. a shared network blip.

Pure and thread-agnostic, same split as `BackoffPolicy`: no sleeping happens here, and
`random_fn` is passed into `next_delay_seconds()` per call (not fixed at construction)
so tests can supply a deterministic source without subclassing or monkeypatching the
`random` module.
"""

from __future__ import annotations

import random
from collections.abc import Callable

_DEFAULT_BASE_DELAY_SECONDS = 2.0
_DEFAULT_MAX_DELAY_SECONDS = 120.0
_DEFAULT_MULTIPLIER = 2.0
_DEFAULT_MAX_ATTEMPTS = 8
_DEFAULT_JITTER_RATIO = 0.2


class ReconnectBackoffPolicy:
    """`base_delay_seconds * (multiplier ** (attempt - 1))`, clamped to
    `max_delay_seconds`, then perturbed by up to `+/- jitter_ratio` of that clamped
    value. After `max_attempts` failed attempts, `is_exhausted` becomes True and
    `next_delay_seconds()` raises — the caller must stop retrying and surface a
    terminal failure requiring manual intervention (never resent/retried forever)."""

    def __init__(
        self,
        *,
        base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS,
        max_delay_seconds: float = _DEFAULT_MAX_DELAY_SECONDS,
        multiplier: float = _DEFAULT_MULTIPLIER,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        jitter_ratio: float = _DEFAULT_JITTER_RATIO,
    ) -> None:
        if base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if multiplier <= 1:
            raise ValueError("multiplier must be > 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not (0 <= jitter_ratio <= 1):
            raise ValueError("jitter_ratio must be between 0 and 1")

        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._multiplier = multiplier
        self._max_attempts = max_attempts
        self._jitter_ratio = jitter_ratio
        self._attempt = 0
        self._cancelled = False

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def attempt_count(self) -> int:
        return self._attempt

    @property
    def is_exhausted(self) -> bool:
        return self._attempt >= self._max_attempts

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def record_failure(self) -> None:
        """Call once per failed (or presumed-transient-disconnect) attempt, before
        requesting the next delay."""
        self._attempt += 1

    def next_delay_seconds(self, *, random_fn: Callable[[], float] = random.random) -> float:
        """`random_fn` must return a value in `[0, 1)` (`random.random`'s own
        contract) — the delay's jitter component is `base * jitter_ratio *
        (2*random_fn() - 1)`, i.e. uniformly distributed in
        `[-base*jitter_ratio, +base*jitter_ratio]`, then floored at 0.

        Raises `RuntimeError` if `is_exhausted` — check that first."""
        if self.is_exhausted:
            raise RuntimeError(
                f"reconnect backoff exhausted after {self._attempt} attempts "
                f"(max_attempts={self._max_attempts})"
            )
        base = min(
            self._base_delay_seconds * (self._multiplier ** (self._attempt - 1)),
            self._max_delay_seconds,
        )
        jitter = base * self._jitter_ratio * (2 * random_fn() - 1)
        return max(0.0, base + jitter)

    def cancel(self) -> None:
        self._cancelled = True

    def reset(self) -> None:
        """Call after a successful attempt to reset attempt count for future use."""
        self._attempt = 0
        self._cancelled = False


__all__ = ["ReconnectBackoffPolicy"]
