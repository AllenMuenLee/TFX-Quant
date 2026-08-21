from __future__ import annotations

import pytest

from tfx_quant.domain.reconnect_backoff import ReconnectBackoffPolicy


def _no_jitter() -> float:
    """Midpoint of `random_fn`'s `[0, 1)` range => zero jitter contribution."""
    return 0.5


def test_first_delay_equals_base_delay_with_zero_jitter() -> None:
    policy = ReconnectBackoffPolicy(base_delay_seconds=1.0, multiplier=2.0, max_attempts=5)
    policy.record_failure()
    assert policy.next_delay_seconds(random_fn=_no_jitter) == 1.0


def test_delay_grows_exponentially_with_zero_jitter() -> None:
    policy = ReconnectBackoffPolicy(base_delay_seconds=1.0, multiplier=2.0, max_attempts=10)
    delays = []
    for _ in range(4):
        policy.record_failure()
        delays.append(policy.next_delay_seconds(random_fn=_no_jitter))
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_delay_is_capped_at_max_delay_seconds() -> None:
    policy = ReconnectBackoffPolicy(
        base_delay_seconds=1.0, max_delay_seconds=5.0, multiplier=2.0, max_attempts=10
    )
    for _ in range(5):
        policy.record_failure()
    assert policy.next_delay_seconds(random_fn=_no_jitter) == 5.0


def test_jitter_perturbs_the_delay_within_the_configured_ratio() -> None:
    policy = ReconnectBackoffPolicy(
        base_delay_seconds=10.0, multiplier=2.0, max_attempts=5, jitter_ratio=0.2
    )
    policy.record_failure()
    high = policy.next_delay_seconds(random_fn=lambda: 1.0)  # upper edge of random_fn's range
    low = policy.next_delay_seconds(random_fn=lambda: 0.0)  # lower edge
    mid = policy.next_delay_seconds(random_fn=_no_jitter)
    assert mid == 10.0
    assert low == pytest.approx(8.0)
    assert high == pytest.approx(12.0)


def test_jitter_never_produces_a_negative_delay() -> None:
    policy = ReconnectBackoffPolicy(
        base_delay_seconds=1.0, multiplier=2.0, max_attempts=5, jitter_ratio=1.0
    )
    policy.record_failure()
    assert policy.next_delay_seconds(random_fn=lambda: 0.0) == 0.0


def test_two_policies_given_different_random_sources_desynchronize() -> None:
    """The whole point of jitter ("避免登入風暴") — two policies retrying the exact
    same attempt count must not always compute the exact same delay."""
    policy_a = ReconnectBackoffPolicy(base_delay_seconds=10.0, max_attempts=5, jitter_ratio=0.5)
    policy_b = ReconnectBackoffPolicy(base_delay_seconds=10.0, max_attempts=5, jitter_ratio=0.5)
    policy_a.record_failure()
    policy_b.record_failure()
    delay_a = policy_a.next_delay_seconds(random_fn=lambda: 0.1)
    delay_b = policy_b.next_delay_seconds(random_fn=lambda: 0.9)
    assert delay_a != delay_b


def test_is_exhausted_after_max_attempts() -> None:
    policy = ReconnectBackoffPolicy(max_attempts=3)
    assert not policy.is_exhausted
    for _ in range(3):
        policy.record_failure()
    assert policy.is_exhausted


def test_next_delay_seconds_raises_once_exhausted() -> None:
    policy = ReconnectBackoffPolicy(max_attempts=1)
    policy.record_failure()
    with pytest.raises(RuntimeError):
        policy.next_delay_seconds()


def test_cancel_sets_is_cancelled() -> None:
    policy = ReconnectBackoffPolicy()
    assert not policy.is_cancelled
    policy.cancel()
    assert policy.is_cancelled


def test_reset_clears_attempt_count_and_cancellation() -> None:
    policy = ReconnectBackoffPolicy(max_attempts=2)
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
        {"jitter_ratio": -0.1},
        {"jitter_ratio": 1.1},
    ],
)
def test_invalid_construction_raises(kwargs: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        ReconnectBackoffPolicy(**kwargs)  # type: ignore[arg-type]
