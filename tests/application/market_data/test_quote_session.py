from datetime import datetime

import pytest

from tfx_quant.application.market_data.quote_session import (
    QuoteSession,
    parse_match_time,
    quote_port,
    quote_request_type,
    quote_session_at,
)
from tfx_quant.application.ports.quote_gateway import QuoteRequestType
from tfx_quant.domain.timestamp import TAIPEI_TZ


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (8, 45, QuoteSession.T),
        (13, 44, QuoteSession.T),
        (15, 0, QuoteSession.T_PLUS_1),
        (23, 0, QuoteSession.T_PLUS_1),
        (4, 59, QuoteSession.T_PLUS_1),
        (6, 0, None),
    ],
)
def test_quote_session_at(hour: int, minute: int, expected: QuoteSession | None) -> None:
    assert quote_session_at(datetime(2026, 8, 24, hour, minute, tzinfo=TAIPEI_TZ)) is expected


def test_sample_ports_and_request_types() -> None:
    """`YuantaQuoteAPI Sample.py` logs in on 80/reqType=1 and 82/reqType=2."""
    assert quote_port(QuoteSession.T) == 80
    assert quote_port(QuoteSession.T_PLUS_1) == 82
    assert quote_request_type(QuoteSession.T) is QuoteRequestType.T
    assert quote_request_type(QuoteSession.T_PLUS_1) is QuoteRequestType.T_PLUS_1


def test_match_time_is_twelve_digits_with_microseconds() -> None:
    received = datetime(2026, 8, 28, 9, 48, 38, tzinfo=TAIPEI_TZ)
    assert parse_match_time("094838038000", received) == datetime(
        2026, 8, 28, 9, 48, 38, 38000, tzinfo=TAIPEI_TZ
    )


def test_night_session_match_time_before_midnight_keeps_previous_date() -> None:
    received = datetime(2026, 8, 29, 0, 0, 2, tzinfo=TAIPEI_TZ)
    assert parse_match_time("235959123456", received) == datetime(
        2026, 8, 28, 23, 59, 59, 123456, tzinfo=TAIPEI_TZ
    )


def test_night_session_match_time_after_midnight_rolls_forward() -> None:
    received = datetime(2026, 8, 28, 23, 59, 59, tzinfo=TAIPEI_TZ)
    assert parse_match_time("000001000000", received) == datetime(
        2026, 8, 29, 0, 0, 1, tzinfo=TAIPEI_TZ
    )


@pytest.mark.parametrize("raw", ["09:48:38", "0948380380001", "09483803800a", "", "2548380000"])
def test_undocumented_match_time_syntax_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_match_time(raw, datetime(2026, 8, 28, 9, 48, tzinfo=TAIPEI_TZ))
