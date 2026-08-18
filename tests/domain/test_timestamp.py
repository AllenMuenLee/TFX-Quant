from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tfx_quant.domain.errors import InvalidTimestampError
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


def test_timestamp_accepts_taipei_tz_aware_datetime() -> None:
    dt = datetime(2026, 8, 14, 9, 30, tzinfo=TAIPEI_TZ)
    assert Timestamp(dt).value == dt


def test_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(InvalidTimestampError):
        Timestamp(datetime(2026, 8, 14, 9, 30))


def test_timestamp_rejects_non_taipei_tz() -> None:
    with pytest.raises(InvalidTimestampError):
        Timestamp(datetime(2026, 8, 14, 9, 30, tzinfo=ZoneInfo("UTC")))


def test_timestamp_now_is_taipei() -> None:
    assert Timestamp.now().value.tzinfo is not None
