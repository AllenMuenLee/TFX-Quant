from __future__ import annotations

import pytest

from tfx_quant.infrastructure.yuanta.backoff import BackoffPolicy


def test_first_delay_equals_base_delay() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, multiplier=2.0, max_attempts=5)
    policy.record_failure()
    assert policy.next_delay_seconds() == 1.0


def test_delay_grows_exponentially() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, multiplier=2.0, max_attempts=10)
    delays = []
    for _ in range(4):
        policy.record_failure()
        delays.append(policy.next_delay_seconds())
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_delay_is_capped_at_max_delay_seconds() -> None:
    policy = BackoffPolicy(
        base_delay_seconds=1.0, max_delay_seconds=5.0, multiplier=2.0, max_attempts=10
    )
    for _ in range(5):
        policy.record_failure()
    assert policy.next_delay_seconds() == 5.0


def test_is_exhausted_after_max_attempts() -> None:
    policy = BackoffPolicy(max_attempts=3)
    assert not policy.is_exhausted
    for _ in range(3):
        policy.record_failure()
    assert policy.is_exhausted


def test_next_delay_seconds_raises_once_exhausted() -> None:
    policy = BackoffPolicy(max_attempts=1)
    policy.record_failure()
    with pytest.raises(RuntimeError):
        policy.next_delay_seconds()


def test_cancel_sets_is_cancelled() -> None:
    policy = BackoffPolicy()
    assert not policy.is_cancelled
    policy.cancel()
    assert policy.is_cancelled


def test_reset_clears_attempt_count_and_cancellation() -> None:
    policy = BackoffPolicy(max_attempts=2)
    policy.record_failure()
    policy.cancel()
    policy.reset()
    assert policy.attempt_count == 0
    assert not policy.is_cancelled
    assert not policy.is_exhausted


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_delay_seconds": 0},
        {"base_delay_seconds": 1.0, "max_delay_seconds": 0.5},
        {"multiplier": 1.0},
        {"max_attempts": 0},
    ],
)
def test_invalid_construction_raises(kwargs: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(**kwargs)  # type: ignore[arg-type]
