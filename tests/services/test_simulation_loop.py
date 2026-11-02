"""Tests for the simulation loop lifecycle."""

import threading
import time

import pytest

from satgonetem.services.mixins.simulation_loop import SimulationLoopMixin


class Harness(SimulationLoopMixin):
    """Minimal host for the mixin with no real topology."""

    def __init__(self) -> None:
        self.running = False
        self.project_name = "test"
        self.update_time = 60
        self.update_factor = 1.0
        self._stop_evt = threading.Event()
        self._sim_thread = None
        self._lifecycle_lock = threading.Lock()
        self.ticks = 0
        self._ticks_lock = threading.Lock()

    def update_simulation(self) -> None:
        with self._ticks_lock:
            self.ticks += 1


def _wait_for_ticks(harness: Harness, count: int = 1, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while harness.ticks < count and time.time() < deadline:
        time.sleep(0.01)


def test_start_requires_project():
    harness = Harness()
    harness.project_name = ""
    with pytest.raises(RuntimeError, match="Open or create a project first"):
        harness.start()


def test_stop_ends_the_loop():
    harness = Harness()
    harness.start()
    _wait_for_ticks(harness)

    harness.stop()

    assert harness.get_running() is False
    assert harness._sim_thread is not None
    assert not harness._sim_thread.is_alive()


def test_each_run_uses_a_new_event():
    harness = Harness()
    harness.start()
    first_evt = harness._stop_evt
    harness.stop()

    harness.start()
    try:
        assert harness._stop_evt is not first_evt
    finally:
        harness.stop()


def test_start_joins_a_previous_run():
    harness = Harness()
    harness.start()
    first_thread = harness._sim_thread

    # Reproduce the window where stop() has set running=False but the old
    # loop is still alive.
    harness.set_running(False)
    harness.start()
    try:
        assert first_thread is not None
        assert not first_thread.is_alive()
    finally:
        harness.stop()


def test_concurrent_start_stop_does_not_hang():
    harness = Harness()
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                harness.start()
                harness.stop()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=10.0)

    assert not thread.is_alive(), "start/stop deadlocked"
    assert errors == []
    harness.stop()
