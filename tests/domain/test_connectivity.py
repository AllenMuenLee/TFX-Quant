from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tfx_quant.domain.connectivity import ChannelHealth, ChannelId, clock_skew_seconds
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


def _ts(hour: int, minute: int = 0, second: int = 0) -> Timestamp:
    return Timestamp(datetime(2026, 8, 21, hour, minute, second, tzinfo=TAIPEI_TZ))


def test_channel_health_initial_is_disconnected_and_stale() -> None:
    health = ChannelHealth.initial(ChannelId.MARKET_DATA)
    assert health.connected is False
    assert health.is_stale is True
    assert health.last_message_at is None
    assert health.last_heartbeat_at is None
    assert health.latency_ms is None
    assert health.last_error is None
    assert health.is_healthy is False


def test_channel_health_is_healthy_requires_connected_fresh_and_error_free() -> None:
    base = ChannelHealth(
        channel=ChannelId.TRADE,
        connected=True,
        last_message_at=_ts(9),
        last_heartbeat_at=_ts(9),
        latency_ms=10.0,
        last_error=None,
        is_stale=False,
    )
    assert base.is_healthy is True

    assert replace(base, connected=False).is_healthy is False
    assert replace(base, is_stale=True).is_healthy is False
    assert replace(base, last_error="boom").is_healthy is False


def test_clock_skew_seconds_is_symmetric_and_absolute() -> None:
    local_at = _ts(9, 0, 0)
    remote_at = _ts(9, 0, 7)
    assert clock_skew_seconds(local_at, remote_at) == 7.0
    assert clock_skew_seconds(remote_at, local_at) == 7.0


def test_clock_skew_seconds_is_zero_for_matching_timestamps() -> None:
    ts = _ts(9, 30, 15)
    assert clock_skew_seconds(ts, ts) == 0.0
