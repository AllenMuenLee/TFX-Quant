from datetime import datetime

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
