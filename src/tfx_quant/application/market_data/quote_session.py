"""Confirmed Yuanta quote-session routing and MatchTime normalization."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from enum import StrEnum

from tfx_quant.application.ports.quote_gateway import QuoteRequestType
from tfx_quant.domain.timestamp import TAIPEI_TZ

QUOTE_HOST = "apiquote.yuantafutures.com.tw"

_MATCH_TIME_LENGTH = 12
_HALF_DAY = timedelta(hours=12)


class QuoteSession(StrEnum):
    T = "T"
    T_PLUS_1 = "T+1"


def quote_session_at(value: datetime) -> QuoteSession | None:
    """Return the active TXF/MXF feed session using Taipei wall-clock time."""
    local = value.astimezone(TAIPEI_TZ)
    wall = local.time().replace(tzinfo=None)
    if time(8, 45) <= wall < time(13, 45):
        return QuoteSession.T
    if wall >= time(15, 0) or wall < time(5, 0):
        return QuoteSession.T_PLUS_1
    return None


def quote_port(session: QuoteSession) -> int:
    """Port per session, taken from ``YuantaQuoteAPI Sample.py``.

    The sample logs in on 80 (T) and 82 (T+1).  ``使用說明.txt`` also lists 443 and 442
    as alternatives, but the installed OCX opens no socket at all on those two — it
    logs the request and stops — so only the sample's ports are used here.
    """
    return 80 if session is QuoteSession.T else 82


def quote_request_type(session: QuoteSession) -> QuoteRequestType:
    """``reqType=1 T盤 , reqType=2 T+1盤`` (``YuantaQuoteAPI Sample.py``)."""
    return QuoteRequestType.T if session is QuoteSession.T else QuoteRequestType.T_PLUS_1


def parse_match_time(raw: str, received_at: datetime) -> datetime:
    """Parse the 12-digit ``MatchTime`` the installed control emits.

    Observed live: ``'094838038000'`` -> 09:48:38.038000, i.e. ``HHMMSS`` followed by
    six fractional digits (microseconds).  ``元大行情API.pdf`` never defines the
    syntax; its own version table records "證券盤前揭示+時間12碼" for 2.0.1.1, and the
    installed control is 2.1.2.9.

    The callback carries no date, so the Taipei calendar date is taken from
    ``received_at``.  The T+1 session spans midnight, so a value more than 12 hours
    away from the receive time is attributed to the adjacent day.
    """
    text = raw.strip()
    if len(text) != _MATCH_TIME_LENGTH or not text.isdigit():
        raise ValueError(f"MatchTime must be {_MATCH_TIME_LENGTH} digits, got {raw!r}")
    hour, minute, second = int(text[0:2]), int(text[2:4]), int(text[4:6])
    microsecond = int(text[6:12])
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"MatchTime is not a valid time of day: {raw!r}")
    received_local = received_at.astimezone(TAIPEI_TZ)
    matched = datetime.combine(
        received_local.date(),
        time(hour, minute, second, microsecond),
        tzinfo=TAIPEI_TZ,
    )
    if matched - received_local > _HALF_DAY:
        return matched - timedelta(days=1)
    if received_local - matched > _HALF_DAY:
        return matched + timedelta(days=1)
    return matched
