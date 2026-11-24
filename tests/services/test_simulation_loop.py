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
        self.active: list = []

    def remove_inter_satellite_link(self, link) -> None:
        self.removed.append(link)
        if link in self.active:
            self.active.remove(link)

    def remove_ground_station_link(self, link) -> None:
        self.removed.append(link)
        if link in self.active:
            self.active.remove(link)

    def remove_user_terminal_link(self, link) -> None:
        self.removed.append(link)
        if link in self.active:
            self.active.remove(link)

    def get_inter_satellites_links(self):
        return [
            link
            for link in self.active
            if getattr(link, "type", "") == "InterSatelliteLink"
        ]

    def get_ground_stations_links(self):
        return [
            link
            for link in self.active
            if getattr(link, "type", "") == "GroundStationLink"
        ]

    def get_user_terminal_links(self):
        return [
            link
            for link in self.active
            if getattr(link, "type", "") == "UserTerminalLink"
        ]


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
    source = SimpleNamespace(name="Sat0")
    target = SimpleNamespace(name="Sat1")
    return SimpleNamespace(
        satcom_object=SimpleNamespace(
            type=link_type,
            source=source,
            destination=target,
        ),
        source=source,
        target=target,
    )


class _FailingRemovalSimulationManager(_FakeSimulationManager):
    """Fail a batch removal partway and record compensating restores."""

    def __init__(self, *, fail_ground_station_removal: bool = True) -> None:
        super().__init__()
        self.fail_ground_station_removal = fail_ground_station_removal
        self.restored: list[tuple] = []

    def remove_ground_station_link(self, link) -> None:
        if self.fail_ground_station_removal:
            raise RuntimeError("removal failed")
        super().remove_ground_station_link(link)

    def create_and_add_inter_satellite_link_connection(
        self, source, destination, inter_satellite_link_direction=None
    ):
        self.restored.append((source, destination, inter_satellite_link_direction))
        return SimpleNamespace(type="InterSatelliteLink")

    def create_and_add_ground_station_link_connection(self, source, destination):
        self.restored.append((source, destination, None))
        return SimpleNamespace(type="GroundStationLink")


class _FailingUpdateHarness(LinkFailureHarness):
    """Raise on the first topology update, then allow the compensating one."""

    def __init__(self) -> None:
        super().__init__()
        self.simulation_manager = _FailingRemovalSimulationManager(
            fail_ground_station_removal=False
        )

    def update_simulation(self, advance_timestep: bool = True) -> None:
        self.updates += 1
        if self.updates == 1:
            raise RuntimeError("update failed")


class _PartialRemovalSimulationManager(_FailingRemovalSimulationManager):
    """Remove a link, then raise, simulating a partially applied removal."""

    def remove_ground_station_link(self, link) -> None:
        super().remove_ground_station_link(link)
        raise RuntimeError("removal failed after mutation")


def test_partial_removal_is_rolled_back():
    harness = LinkFailureHarness()
    harness.simulation_manager = _PartialRemovalSimulationManager(
        fail_ground_station_removal=False
    )
    first = _batch_link("InterSatelliteLink")
    second = _batch_link("GroundStationLink")
    harness.simulation_manager.active = [first.satcom_object, second.satcom_object]

    with pytest.raises(RuntimeError, match="removal failed after mutation"):
        harness.simulate_link_failure([first, second])

    assert harness.simulation_manager.removed == [
        first.satcom_object,
        second.satcom_object,
    ]
    assert harness.simulation_manager.restored == [
        (second.satcom_object.source, second.satcom_object.destination, None),
        (first.satcom_object.source, first.satcom_object.destination, None),
    ]


def test_mid_batch_removal_failure_rolls_back_removed_links_and_updates():
    """A failed removal must compensate earlier removals and still sync."""
    harness = LinkFailureHarness()
    harness.simulation_manager = _FailingRemovalSimulationManager()
    first = _batch_link("InterSatelliteLink")
    second = _batch_link("GroundStationLink")
    harness.simulation_manager.active = [first.satcom_object, second.satcom_object]

    with pytest.raises(RuntimeError, match="removal failed"):
        harness.simulate_link_failure([first, second])

    satcom = first.satcom_object
    assert harness.simulation_manager.removed == [satcom]
    assert harness.simulation_manager.restored == [
        (satcom.source, satcom.destination, None)
    ]
    # The compensating update ran even though the batch failed.
    assert harness.updates == 1


def test_update_failure_rolls_back_the_whole_batch_and_syncs_again():
    harness = _FailingUpdateHarness()
    first = _batch_link("InterSatelliteLink")
    second = _batch_link("GroundStationLink")

    with pytest.raises(RuntimeError, match="update failed"):
        harness.simulate_link_failure([first, second])

    assert len(harness.simulation_manager.removed) == 2
    assert harness.simulation_manager.restored == [
        (second.satcom_object.source, second.satcom_object.destination, None),
        (first.satcom_object.source, first.satcom_object.destination, None),
    ]
    # One failing update plus the post-rollback update.
    assert harness.updates == 2


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


def test_move_to_time_syncs_topology_before_rebuilding_routing():
    from datetime import datetime, timedelta

    events: list[tuple[str, bool]] = []

    class MoveHarness(SimulationLoopMixin):
        def __init__(self) -> None:
            self.start_time = datetime(2026, 1, 1, 0, 0, 0)
            self.update_time = 60
            self.current_time_step = 0
            self.routing_daemon = None
            self._simulation_lock = threading.RLock()
            self.simulation_manager = SimpleNamespace(
                time_manager=SimpleNamespace(
                    set_time=lambda t: events.append(
                        ("set_time", self._simulation_lock._is_owned())
                    ),
                    execute_actions=lambda: events.append(
                        ("execute_actions", self._simulation_lock._is_owned())
                    ),
                )
            )

        def update_simulation(self, advance_timestep: bool = True) -> None:
            assert advance_timestep is False
            events.append(("update_simulation", self._simulation_lock._is_owned()))

        def rebuild_routing_for_current_timestep(self) -> None:
            events.append(("rebuild_routing", self._simulation_lock._is_owned()))

    harness = MoveHarness()
    target = harness.start_time + timedelta(seconds=180)
    harness.move_to_time(target)

    assert harness.current_time_step == 3
    assert events == [
        ("set_time", True),
        ("execute_actions", True),
        ("update_simulation", True),
        ("rebuild_routing", True),
    ]
