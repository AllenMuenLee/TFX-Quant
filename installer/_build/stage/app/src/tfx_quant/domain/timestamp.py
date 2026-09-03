"""時間戳 — a tz-aware instant, required to be in Asia/Taipei.

All EOD-flatten / no-entry-window / trading-session logic (Features 04-10) reasons
about wall-clock time in Asia/Taipei, so naive datetimes and other time zones are
rejected at construction rather than trusted implicitly at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tfx_quant.domain.errors import InvalidTimestampError

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
_TAIPEI_UTC_OFFSET = timedelta(hours=8)  # Asia/Taipei has no DST — this offset is constant.


@dataclass(frozen=True, slots=True)
class Timestamp:
    """A datetime that is guaranteed tz-aware and in Asia/Taipei."""

    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None:
            raise InvalidTimestampError("timestamp must be tz-aware, got a naive datetime")
        offset = self.value.utcoffset()
        if offset != _TAIPEI_UTC_OFFSET:
            raise InvalidTimestampError(
                f"timestamp must be in Asia/Taipei (UTC+08:00), got offset {offset}"
            )

    @classmethod
    def now(cls) -> Timestamp:
        return cls(datetime.now(TAIPEI_TZ))
