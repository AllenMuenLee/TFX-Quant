from __future__ import annotations

import threading
import time as time_module

import pytest

from tfx_quant.application.events.event_coordinator import EventCoordinator
from tfx_quant.application.events.events import Event, UnhandledHandlerError
from tfx_quant.domain.timestamp import Timestamp


@pytest.fixture
def coordinator() -> EventCoordinator:
    coord = EventCoordinator()
    coord.start()
    yield coord
    coord.stop(timeout=5)


def test_handlers_receive_published_events_in_order(coordinator: EventCoordinator) -> None:
    received: list[Event] = []
    done = threading.Event()

    def handler(event: Event) -> None:
        received.append(event)
        if len(received) == 5:
            done.set()

    coordinator.subscribe(Event, handler)
    events = [Event(at=Timestamp.now()) for _ in range(5)]
    for event in events:
        coordinator.publish(event)

    assert done.wait(timeout=2)
    assert received == events


def test_handlers_never_run_concurrently_with_each_other(coordinator: EventCoordinator) -> None:
    """Publish from many threads; an unsynchronized counter must still end up correct,
    which is only true if the coordinator serializes all handler invocations."""
    counter = {"value": 0}
    done = threading.Event()
    total_events = 200

    def handler(_event: Event) -> None:
        # Deliberately unsynchronized read-modify-write — would race if the
        # coordinator ever invoked two handlers concurrently.
        current = counter["value"]
        counter["value"] = current + 1
        if counter["value"] == total_events:
            done.set()

    coordinator.subscribe(Event, handler)

    def publish_many() -> None:
        for _ in range(total_events // 4):
            coordinator.publish(Event(at=Timestamp.now()))

    threads = [threading.Thread(target=publish_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert done.wait(timeout=5)
    assert counter["value"] == total_events


def test_handler_exception_is_isolated_and_reported_as_event(
    coordinator: EventCoordinator,
) -> None:
    error_received = threading.Event()
    captured: list[UnhandledHandlerError] = []

    def failing_handler(_event: Event) -> None:
        raise ValueError("boom")

    def error_handler(event: UnhandledHandlerError) -> None:
        captured.append(event)
        error_received.set()

    coordinator.subscribe(Event, failing_handler)
    coordinator.subscribe(UnhandledHandlerError, error_handler)

    coordinator.publish(Event(at=Timestamp.now()))

    assert error_received.wait(timeout=2)
    assert len(captured) == 1
    assert isinstance(captured[0].error, ValueError)


def test_unsubscribe_stops_further_delivery(coordinator: EventCoordinator) -> None:
    received: list[Event] = []
    unsubscribe = coordinator.subscribe(Event, received.append)
    unsubscribe()

    coordinator.publish(Event(at=Timestamp.now()))
    time_module.sleep(0.2)

    assert received == []


def test_starting_twice_raises() -> None:
    coord = EventCoordinator()
    coord.start()
    try:
        with pytest.raises(RuntimeError):
            coord.start()
    finally:
        coord.stop(timeout=5)
