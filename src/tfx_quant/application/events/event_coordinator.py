"""EventCoordinator — the single serialized event-processing queue.

Broker callbacks (COM/OCX thread), the strategy loop, and the UI must never call each
other directly. Instead every side effect becomes an `Event`, published (thread-safe,
callable from any thread) onto one internal queue, drained by exactly one dedicated
consumer thread. Every subscriber handler therefore runs on that single thread, one at
a time, in publish order — no two handlers ever race each other, and no handler ever
runs concurrently with itself.

A handler exception is caught and turned into an `UnhandledHandlerError` event rather
than crashing the dispatch loop or being silently swallowed, so application code can
subscribe to it and route the failure toward a safe-pause / Faulted transition (see
the global rule: "任何未捕捉例外應轉入安全暫停").
"""

from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from tfx_quant.application.events.events import Event, UnhandledHandlerError
from tfx_quant.domain.timestamp import Timestamp
from tfx_quant.telemetry import get_logger, log_debug, log_error

Handler = Callable[[Any], None]
Unsubscribe = Callable[[], None]

_STOP = object()

_logger = get_logger(__name__)


class EventCoordinator:
    def __init__(self) -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self._subscribers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._subscribers_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    def subscribe(self, event_type: type[Event], handler: Handler) -> Unsubscribe:
        with self._subscribers_lock:
            self._subscribers[event_type].append(handler)

        def unsubscribe() -> None:
            with self._subscribers_lock:
                handlers = self._subscribers.get(event_type)
                if handlers is not None and handler in handlers:
                    handlers.remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        """Thread-safe. Callable from the COM callback thread, strategy thread, etc."""
        self._queue.put(event)
        # DEBUG-only (not INFO): this path also carries `MarketDataTickReceived`, so an
        # always-on INFO log here would be a per-tick firehose blocking the publishing
        # callback thread — see Feature 04's "避免逐 tick 的 INFO log 阻塞 callback".
        log_debug(
            _logger,
            "event_enqueued",
            event_type=type(event).__name__,
            queue_depth=self._queue.qsize(),
            source_thread=threading.current_thread().name,
        )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("EventCoordinator is already started")
        self._thread = threading.Thread(target=self._run, name="EventCoordinator", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            assert isinstance(item, Event)
            self._dispatch(item)

    def _dispatch(self, event: Event) -> None:
        event_type = type(event).__name__
        log_debug(
            _logger,
            "event_dequeued",
            event_type=event_type,
            queue_depth=self._queue.qsize(),
            source_thread=threading.current_thread().name,
        )
        start = time.monotonic()
        with self._subscribers_lock:
            handlers = [
                handler
                for event_type_key, subs in self._subscribers.items()
                if isinstance(event, event_type_key)
                for handler in subs
            ]
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - isolate one bad handler from the loop
                self._on_handler_error(event, exc)
        log_debug(
            _logger,
            "event_processed",
            event_type=event_type,
            handler_count=len(handlers),
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def _on_handler_error(self, source_event: Event, error: BaseException) -> None:
        log_error(
            _logger,
            "event_handler_failed",
            event_type=type(source_event).__name__,
            error_type=type(error).__name__,
            error=str(error),
        )
        if isinstance(source_event, UnhandledHandlerError):
            # A handler of UnhandledHandlerError itself failed — do not loop forever.
            return
        self.publish(
            UnhandledHandlerError(at=Timestamp.now(), source_event=source_event, error=error)
        )
