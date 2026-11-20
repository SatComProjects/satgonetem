"""Regression tests for flow scheduler timeout handling (TRAFFIC-16).

A timed-out flow must be stopped (its tool process killed and its thread
joined) before the worker is released, and the default configuration must
not truncate legitimate long-running tests such as a 300 s iperf3 run.
"""

from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest

from satgonetem.traffic.flow_scheduler import FlowScheduler, FlowSchedulerStatus

# ---------------------------------------------------------------------------
# Live-flow tracking shared by the fake flows. Used to assert that the
# scheduler never has more fake flow threads alive than max_workers.
# ---------------------------------------------------------------------------

_TRACK_LOCK = threading.Lock()
_LIVE_FLOWS = 0
_MAX_LIVE_FLOWS = 0


def _reset_tracking() -> None:
    global _LIVE_FLOWS, _MAX_LIVE_FLOWS
    with _TRACK_LOCK:
        _LIVE_FLOWS = 0
        _MAX_LIVE_FLOWS = 0


def _enter_flow() -> None:
    global _LIVE_FLOWS, _MAX_LIVE_FLOWS
    with _TRACK_LOCK:
        _LIVE_FLOWS += 1
        _MAX_LIVE_FLOWS = max(_MAX_LIVE_FLOWS, _LIVE_FLOWS)


def _exit_flow() -> None:
    global _LIVE_FLOWS
    with _TRACK_LOCK:
        _LIVE_FLOWS -= 1


class _Node:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeFlow:
    """Minimal flow whose background thread waits until stop() is called."""

    def __init__(self, name: str, *, wait_for_stop: bool = True) -> None:
        self.source = _Node(name)
        self.destination = _Node("dst")
        self.delay = 0.0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.started_event = threading.Event()
        self.stop_calls = 0
        self._result = f"result-{name}"
        self._wait_for_stop = wait_for_stop
        self.first_thread_alive_at_start: bool | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        _enter_flow()
        try:
            self.started_event.set()
            if self._wait_for_stop:
                self._stop_event.wait()
        finally:
            _exit_flow()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop_event.set()

    def results(self) -> str:
        return self._result


class _WatchingFlow(_FakeFlow):
    """Flow that records whether another flow's thread was alive at start."""

    def __init__(self, name: str, other: _FakeFlow, **kwargs) -> None:
        super().__init__(name, **kwargs)
        self._other = other

    def start(self) -> None:
        self.first_thread_alive_at_start = self._other._thread.is_alive()
        super().start()


class _RecordingThread:
    """Fake thread that records the join timeout used by the scheduler."""

    def __init__(self) -> None:
        self.join_timeouts: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return False


class _NoTruncationFlow:
    """Fake iperf3-style flow configured for a 300 s test.

    The thread is already finished, so the scheduler never waits for real
    time; the join timeout it passes reveals whether the default truncates.
    """

    def __init__(self, duration: float = 300.0) -> None:
        self.source = _Node("Iperf3Flow")
        self.destination = _Node("dst")
        self.delay = 0.0
        self.config = SimpleNamespace(duration=duration)
        self._thread: _RecordingThread | None = None
        self._result = "iperf3-result"
        self.stop_called = False

    def start(self) -> None:
        self._thread = _RecordingThread()

    def stop(self) -> None:
        self.stop_called = True

    def results(self) -> str:
        return self._result


@pytest.fixture(autouse=True)
def _tracking_fixture():
    _reset_tracking()
    yield
    _reset_tracking()


def test_timeout_stops_flow_and_records_timeout_error():
    """A flow outliving its explicit timeout is stopped before the error."""
    flow = _FakeFlow("slow", wait_for_stop=True)
    scheduler = FlowScheduler([flow], max_workers=1, flow_timeout_sec=0.05)

    try:
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        errors = scheduler.errors()
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
        assert "Flow timed out after 0.05s" in str(errors[0])
        assert flow.stop_calls == 1
        # The scheduler joined the thread again after stop(), so it is gone
        # by the time the worker is released.
        assert flow._thread is not None
        assert not flow._thread.is_alive()
    finally:
        flow._stop_event.set()


def test_default_timeout_is_none_and_does_not_truncate_300s_flow():
    """The default must not impose a 180 s cap on a valid 300 s iperf3 test."""
    default = inspect.signature(FlowScheduler).parameters["flow_timeout_sec"].default
    assert default is None

    flow = _NoTruncationFlow(duration=300.0)
    scheduler = FlowScheduler([flow], max_workers=1)

    assert scheduler._flow_timeout_sec is None

    scheduler.run()
    scheduler.join(timeout=5.0)

    assert scheduler.status() == FlowSchedulerStatus.DONE
    assert scheduler.errors() == []
    assert scheduler.results(flow) == "iperf3-result"
    assert flow.stop_called is False
    assert flow._thread is not None
    # None means "wait indefinitely", so the 300 s duration is never cut off.
    assert flow._thread.join_timeouts == [None]


def test_max_workers_one_stops_timed_out_flow_before_next_flow():
    """With max_workers=1 the timed-out flow is stopped before the next starts."""
    slow = _FakeFlow("slow", wait_for_stop=True)
    second = _WatchingFlow("second", slow, wait_for_stop=False)
    scheduler = FlowScheduler(
        [slow, second], max_workers=1, flow_timeout_sec=0.05
    )

    try:
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert scheduler.status() == FlowSchedulerStatus.DONE
        errors = scheduler.errors()
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)

        # The second flow only starts once the first worker is free, and by
        # then the timed-out flow's thread has been stopped and joined.
        assert second.first_thread_alive_at_start is False
        assert slow.stop_calls == 1
        assert slow._thread is not None
        assert not slow._thread.is_alive()

        # Never more than max_workers flow threads alive at once.
        assert _MAX_LIVE_FLOWS == 1
    finally:
        slow._stop_event.set()
        second._stop_event.set()


def test_explicit_timeout_still_enforced():
    """Explicit flow_timeout_sec values are not ignored by the new default."""
    flow = _FakeFlow("slow", wait_for_stop=True)
    scheduler = FlowScheduler([flow], max_workers=1, flow_timeout_sec=0.05)

    try:
        scheduler.run()
        scheduler.join(timeout=5.0)

        assert len(scheduler.errors()) == 1
        assert isinstance(scheduler.errors()[0], TimeoutError)
    finally:
        flow._stop_event.set()
