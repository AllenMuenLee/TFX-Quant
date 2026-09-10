import json
import logging
from datetime import datetime

import pytest
from pydantic import SecretStr

from tfx_quant.application.market_data.live_quote_service import LiveQuoteService
from tfx_quant.application.ports.quote_gateway import (
    QuoteConnectionState,
    QuoteRequestType,
    QuoteUpdateMode,
)
from tfx_quant.domain.timestamp import TAIPEI_TZ, Timestamp


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> Timestamp:
        return Timestamp(self.value)


class Gateway:
    def __init__(self) -> None:
        self.state = QuoteConnectionState.IDLE
        self.connections: list[tuple[str, int, int]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.unsubscriptions: list[tuple[str, int]] = []
        self.stopped = False
        self.raise_on_stop = False

    def connect(
        self,
        _user: str,
        _password: SecretStr,
        host: str,
        port: int,
        request_type: QuoteRequestType,
    ) -> None:
        self.connections.append((host, port, int(request_type)))
        self.state = QuoteConnectionState.LOGGED_ON

    def subscribe(
        self,
        symbol: str,
        request_type: QuoteRequestType,
        mode: QuoteUpdateMode = QuoteUpdateMode.SNAPSHOT_UPDATE,
    ) -> None:
        self.subscriptions.append((symbol, int(request_type)))

    def unsubscribe(self, symbol: str, request_type: QuoteRequestType) -> None:
        self.unsubscriptions.append((symbol, int(request_type)))
        self.subscriptions = [entry for entry in self.subscriptions if entry[0] != symbol]

    def stop(self) -> None:
        self.stopped = True
        self.state = QuoteConnectionState.STOPPED
        if self.raise_on_stop:
            raise RuntimeError("COMError: DelMktReg rejected, link already closed")


def _service() -> tuple[LiveQuoteService, list[Gateway], Clock]:
    clock = Clock(datetime(2026, 8, 24, 9, 0, tzinfo=TAIPEI_TZ))
    gateways: list[Gateway] = []

    def factory() -> Gateway:
        gateway = Gateway()
        gateways.append(gateway)
        return gateway

    return LiveQuoteService(factory, clock), gateways, clock


def test_switches_from_t_to_t_plus_1_port_and_resubscribes_every_symbol() -> None:
    service, gateways, clock = _service()
    service.start("A123456789", SecretStr("secret"), ("TXFI6", "MXFI6"))
    assert gateways[0].connections == [("apiquote.yuantafutures.com.tw", 80, 1)]
    assert gateways[0].subscriptions == [("TXFI6", 1), ("MXFI6", 1)]

    clock.value = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI_TZ)
    service.refresh()
    assert gateways[0].stopped
    assert gateways[1].connections == [("apiquote.yuantafutures.com.tw", 82, 2)]
    assert gateways[1].subscriptions == [("TXFI6", 2), ("MXFI6", 2)]


def test_intermission_teardown_failure_is_logged_as_past_day_session_not_a_fault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """13:45–15:00 has no feed, so `refresh()` tears the T session down. If the vendor
    control rejects the shutdown (its socket to the T server is already gone), the
    error must be swallowed — it used to bubble out of the market-data refresh timer
    and close the whole application — and recorded plainly as "過了日盤時間", not as a
    warning."""
    caplog.set_level(
        logging.INFO, logger="tfx_quant.application.market_data.live_quote_service"
    )
    service, gateways, clock = _service()
    clock.value = datetime(2026, 8, 24, 13, 0, tzinfo=TAIPEI_TZ)
    service.start("A123456789", SecretStr("secret"), ("TXFI6", "MXFI6"))
    gateways[0].raise_on_stop = True

    clock.value = datetime(2026, 8, 24, 14, 0, tzinfo=TAIPEI_TZ)
    service.refresh()  # must not raise
    assert gateways[0].stopped

    payloads = [json.loads(record.message) for record in caplog.records]
    past_day = next(
        p for p in payloads if p["event"] == "quote_connection_closed_past_day_session"
    )
    assert past_day["note"] == "過了日盤時間"
    assert past_day["scheduled_resume"] == "15:00:00"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_intermission_teardown_failure_still_reconnects_for_the_night_session() -> None:
    service, gateways, clock = _service()
    clock.value = datetime(2026, 8, 24, 13, 0, tzinfo=TAIPEI_TZ)
    service.start("A123456789", SecretStr("secret"), ("TXFI6", "MXFI6"))
    gateways[0].raise_on_stop = True

    clock.value = datetime(2026, 8, 24, 14, 0, tzinfo=TAIPEI_TZ)
    service.refresh()

    clock.value = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI_TZ)
    service.refresh()
    assert gateways[1].connections == [("apiquote.yuantafutures.com.tw", 82, 2)]
    assert gateways[1].subscriptions == [("TXFI6", 2), ("MXFI6", 2)]


def test_dropped_symbol_unregisters_with_the_active_session_request_type() -> None:
    service, gateways, _clock = _service()
    service.start("A123456789", SecretStr("secret"), ("TXFI6", "MXFI6"))
    service.select_symbols(("MXFI6", "TXFL6"))
    assert gateways[0].unsubscriptions == [("TXFI6", 1)]
    assert gateways[0].subscriptions == [("MXFI6", 1), ("TXFL6", 1)]


def test_reselecting_an_unchanged_set_never_touches_the_running_registrations() -> None:
    """An instrument switch that only changes the charted market must not interrupt
    either feed — see `desktop.quote_runtime.QuoteRuntime._on_switch`."""
    service, gateways, _clock = _service()
    service.start("A123456789", SecretStr("secret"), ("TXFI6", "MXFI6"))
    service.select_symbols(("MXFI6", "TXFI6"))
    assert gateways[0].unsubscriptions == []
    assert gateways[0].subscriptions == [("TXFI6", 1), ("MXFI6", 1)]
