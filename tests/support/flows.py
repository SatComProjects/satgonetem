"""Test flows for exercising FlowScheduler without external traffic tools."""

from __future__ import annotations

import threading
from typing import Any


class RecordingFlow:
    """A deterministic stand-in for Ping/Hping3/Iperf3 flows.

    ``FlowScheduler`` interacts with a flow through ``delay``, ``start()``,
    ``_thread``, ``stop()``, ``_lock``, ``_result``, and ``results()``. This
    double implements exactly that surface.
    """

    def __init__(
        self,
        source: Any,
        destination: Any,
        *,
        delay: float = 0.0,
        duration: float = 0.0,
        result: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.source = source
        self.destination = destination
        self.delay = delay
        self.duration = duration
        self.result = result
        self.error = error
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._result: Any = None
        self._error: Exception | None = None
        self._stop_event = threading.Event()
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._stop_event.wait(self.duration)
            if self.error is not None:
                raise self.error
            self._result = self.result
        except Exception as exc:  # surfaced by results() to the scheduler
            self._error = exc

    def stop(self) -> None:
        self.stopped = True
        self._stop_event.set()

    def results(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result
