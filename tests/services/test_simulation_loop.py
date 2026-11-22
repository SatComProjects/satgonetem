"""Tests for the simulation loop lifecycle."""

import threading
import time
from types import SimpleNamespace

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


class _FakeSimulationManager:
    """Record link removals without touching a real sat_com simulation."""

    def __init__(self) -> None:
        self.removed: list = []

    def remove_inter_satellite_link(self, link) -> None:
        self.removed.append(link)

    def remove_ground_station_link(self, link) -> None:
        self.removed.append(link)

    def remove_user_terminal_link(self, link) -> None:
        self.removed.append(link)


class LinkFailureHarness(SimulationLoopMixin):
    """Minimal host for simulate_link_failure with an observed update call."""

    def __init__(self) -> None:
        self.project_name = "test"
        self.update_time = 60
        self.simulation_manager = _FakeSimulationManager()
        self.updates = 0

    def update_simulation(self, advance_timestep: bool = True) -> None:
        self.updates += 1


def _batch_link(link_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        satcom_object=SimpleNamespace(type=link_type),
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )


def test_invalid_link_in_batch_leaves_earlier_links_untouched():
    """Validation must cover the whole batch before any link is removed."""
    harness = LinkFailureHarness()
    valid = _batch_link("InterSatelliteLink")
    invalid = _batch_link("UnsupportedLink")

    with pytest.raises(AttributeError):
        harness.simulate_link_failure([valid, invalid])

    assert harness.simulation_manager.removed == []
    assert harness.updates == 0


def test_valid_batch_removes_every_link_and_updates_once():
    harness = LinkFailureHarness()
    first = _batch_link("InterSatelliteLink")
    second = _batch_link("GroundStationLink")

    harness.simulate_link_failure([first, second])

    assert harness.simulation_manager.removed == [
        first.satcom_object,
        second.satcom_object,
    ]
    assert harness.updates == 1
