"""Regression tests for CORE-04: simulation loop survives and records errors.

A tick that raises must not silently kill the background thread. The loop has
to log the failure, store it in ``last_simulation_error``, keep running so the
next tick retries, and still terminate cleanly on ``stop()``.
"""

from __future__ import annotations

import threading
import time

from satgonetem.services.mixins.simulation_loop import SimulationLoopMixin


class FlakyHarness(SimulationLoopMixin):
    """Lightweight mixin host whose first tick raises, then succeeds."""

    def __init__(self, failures: int = 1) -> None:
        self.running = False
        self.project_name = "core04"
        self.update_time = 0.01
        self.update_factor = 1.0
        self._stop_evt = threading.Event()
        self._sim_thread = None
        self._lifecycle_lock = threading.Lock()
        self.ticks = 0
        self.successful_ticks = 0
        self.failures_remaining = failures
        self.error = RuntimeError("simulated gRPC tick failure")
        self._ticks_lock = threading.Lock()

    def update_simulation(self) -> None:
        with self._ticks_lock:
            self.ticks += 1
            if self.failures_remaining > 0:
                self.failures_remaining -= 1
                raise self.error
            self.successful_ticks += 1


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.005)
    return predicate()


def test_failed_tick_is_recorded_and_loop_keeps_running():
    harness = FlakyHarness(failures=1)

    # Lightweight harnesses do not run TopologyManager.__init__.
    assert harness.get_last_simulation_error() is None

    harness.start()
    try:
        assert _wait_for(lambda: harness.ticks >= 1)
        assert _wait_for(lambda: harness.get_last_simulation_error() is harness.error)

        # The loop kept ticking after the failure instead of terminating.
        assert _wait_for(lambda: harness.successful_ticks >= 2)
        assert harness.ticks >= 3
        assert harness.get_running() is True
        assert harness._sim_thread is not None
        assert harness._sim_thread.is_alive()

        # A later successful tick does not clear the stored error.
        assert harness.get_last_simulation_error() is harness.error
    finally:
        harness.stop()

    assert harness.get_running() is False
    assert harness._sim_thread is not None
    assert not harness._sim_thread.is_alive()
    assert harness.get_last_simulation_error() is harness.error
