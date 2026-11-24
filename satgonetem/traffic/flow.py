"""Common flow protocol and threaded execution base.

Ping, hping3, and iperf3 flows share the same non-blocking lifecycle. The
base class owns thread/status/error handling; subclasses provide only command
execution, process cleanup, and result parsing.
"""

from __future__ import annotations

import abc
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from satgonetem.models.node import Node


@runtime_checkable
class Flow(Protocol):
    """Minimal flow surface used by FlowScheduler."""

    delay: float

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> Any: ...

    def results(self) -> Any: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def discard_result(self) -> None: ...


class ThreadedFlow(abc.ABC):
    """Shared non-blocking flow lifecycle."""

    STATUS: Any
    flow_name = "Flow"

    def __init__(
        self,
        source: Node,
        destination: Node,
        config: Any,
        delay: float = 0.0,
    ) -> None:
        self.source = source
        self.destination = destination
        self.config = config
        self.delay = delay

        self._status = self.STATUS.IDLE
        self._result: Any = None
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._waiting_delay = False
        self._last_raw: str | None = None

    def start(self) -> None:
        """Start the flow in a background daemon thread."""
        with self._lock:
            if self._status != self.STATUS.IDLE:
                raise RuntimeError(
                    f"{self.flow_name} has already been started "
                    f"(status={self._status.value})"
                )
            self._status = self.STATUS.RUNNING
            self._waiting_delay = self.delay > 0.0
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the underlying process and join the flow thread."""
        with self._lock:
            if self._status != self.STATUS.RUNNING:
                return
            self._stop_event.set()
            should_stop_process = not self._waiting_delay
        if should_stop_process:
            self._stop_process()
        self.join(timeout=5.0)

    def status(self):
        """Return the current lifecycle state."""
        with self._lock:
            return self._status

    def results(self):
        """Return parsed results after successful completion."""
        with self._lock:
            if self._status == self.STATUS.IDLE:
                raise RuntimeError(f"{self.flow_name} has not been started yet")
            if self._status == self.STATUS.RUNNING:
                raise RuntimeError(f"{self.flow_name} is still running")
            if self._status == self.STATUS.ERROR:
                raise RuntimeError(
                    f"{self.flow_name} failed: {self._error}"
                ) from self._error
            if self._status == self.STATUS.DONE:
                return self._result
            raise TypeError(f"Unexpected {self.flow_name} status: {self._status!r}")

    def join(self, timeout: float | None = None) -> None:
        """Join the background thread."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        """Return True while the background thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def discard_result(self) -> None:
        """Release the retained result object."""
        with self._lock:
            self._result = None

    def _run(self) -> None:
        if self.delay > 0.0 and self._stop_event.wait(self.delay):
            with self._lock:
                self._waiting_delay = False
                self._error = RuntimeError(
                    f"{self.flow_name} stopped before it could start"
                )
                self._status = self.STATUS.ERROR
            return
        with self._lock:
            if self._waiting_delay and self._stop_event.is_set():
                self._waiting_delay = False
                self._error = RuntimeError(
                    f"{self.flow_name} stopped before it could start"
                )
                self._status = self.STATUS.ERROR
                return
            self._waiting_delay = False
        try:
            result = self._execute()
            with self._lock:
                self._result = result
                self._status = self.STATUS.DONE
        except Exception as exc:
            self._handle_error(exc)
            with self._lock:
                self._error = exc
                self._status = self.STATUS.ERROR

    def _handle_error(self, exc: Exception) -> None:
        if self._stop_event.is_set() and self._last_raw:
            print(self._last_raw)

    @abc.abstractmethod
    def _execute(self) -> Any:
        """Run the tool and return parsed results."""

    @abc.abstractmethod
    def _stop_process(self) -> None:
        """Stop the underlying process inside the source container."""
