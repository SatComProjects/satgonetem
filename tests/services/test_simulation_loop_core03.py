"""Regression tests for CORE-03: serialize API mutations with the loop.

The policy under test is that API-driven topology mutations (``next_step``,
``move_to_time``, ``simulate_link_failure``) do not queue or get rejected:
they block on the per-instance simulation lock until the in-flight loop tick
finishes, then run to completion atomically. These tests instrument slow
mutation sections and assert that no two ever run concurrently.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

from satgonetem.services.mixins.simulation_loop import SimulationLoopMixin
from satgonetem.services.mixins.topology_sync import TopologySyncMixin


class SectionTracker:
    """Record concurrent entries into instrumented mutation sections."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entries = 0
        self.overlaps = 0

    @contextmanager
    def section(self, name: str):
        with self._lock:
            self.active += 1
            self.entries += 1
            if self.active > self.max_active:
                self.max_active = self.active
            if self.active > 1:
                self.overlaps += 1
        try:
            yield name
        finally:
            with self._lock:
                self.active -= 1


class _FakeTimeManager:
    def __init__(self) -> None:
        self.current_time = None

    def set_time(self, new_time: datetime) -> None:
        self.current_time = new_time

    def execute_actions(self) -> None:
        pass


class _FakeSimulationManager:
    """Fake sat_com manager whose removals are slow and instrumented."""

    def __init__(self, tracker: SectionTracker, delay: float) -> None:
        self.tracker = tracker
        self.delay = delay
        self.time_manager = _FakeTimeManager()
        self.removed: list = []
        self._removed_lock = threading.Lock()

    def _record_removal(self, section: str, link) -> None:
        with self.tracker.section(section):
            time.sleep(self.delay)
        with self._removed_lock:
            self.removed.append(link)

    def remove_inter_satellite_link(self, link) -> None:
        self._record_removal("remove_isl", link)

    def remove_ground_station_link(self, link) -> None:
        self._record_removal("remove_gsl", link)

    def remove_user_terminal_link(self, link) -> None:
        self._record_removal("remove_utl", link)


class MutationHarness(SimulationLoopMixin):
    """Mixin host with slow instrumented updates and no real topology."""

    def __init__(self, update_delay: float = 0.03) -> None:
        self.running = False
        self.project_name = "core03"
        self.update_time = 0.01
        self.update_factor = 1.0
        self._stop_evt = threading.Event()
        self._sim_thread = None
        self._lifecycle_lock = threading.Lock()
        self.start_time = datetime(2026, 1, 1)
        self.current_time_step = 0
        self.routing_daemon = None
        self.tracker = SectionTracker()
        self.update_delay = update_delay
        self.simulation_manager = _FakeSimulationManager(
            self.tracker, update_delay / 2
        )

    def update_simulation(self, advance_timestep: bool = True) -> None:
        with self.tracker.section("update"):
            time.sleep(self.update_delay)
            if advance_timestep:
                self.current_time_step += 1

    def rebuild_routing_for_current_timestep(self) -> None:
        with self.tracker.section("rebuild_routing"):
            time.sleep(self.update_delay / 2)


def _link(link_type: str = "InterSatelliteLink") -> SimpleNamespace:
    return SimpleNamespace(
        satcom_object=SimpleNamespace(type=link_type),
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.005)


def test_concurrent_api_mutations_do_not_interleave():
    harness = MutationHarness()
    tracker = harness.tracker
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(6)
    iterations = 3

    def record_error(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    def next_step_worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            for _ in range(iterations):
                harness.next_step()
        except BaseException as exc:  # pragma: no cover - failure path
            record_error(exc)

    def move_worker(offset: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for i in range(iterations):
                harness.move_to_time(
                    harness.start_time + timedelta(seconds=offset + i)
                )
        except BaseException as exc:  # pragma: no cover - failure path
            record_error(exc)

    def failure_worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            for _ in range(iterations):
                harness.simulate_link_failure(_link())
        except BaseException as exc:  # pragma: no cover - failure path
            record_error(exc)

    threads = [
        threading.Thread(target=next_step_worker, daemon=True),
        threading.Thread(target=next_step_worker, daemon=True),
        threading.Thread(target=move_worker, args=(10,), daemon=True),
        threading.Thread(target=move_worker, args=(20,), daemon=True),
        threading.Thread(target=failure_worker, daemon=True),
        threading.Thread(target=failure_worker, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)

    assert not any(thread.is_alive() for thread in threads), "worker deadlocked"
    assert errors == [], f"workers raised: {errors!r}"
    assert tracker.entries > 0
    assert tracker.max_active == 1, (
        f"simulation mutations overlapped: max concurrency {tracker.max_active}"
    )
    assert tracker.overlaps == 0
    assert len(harness.simulation_manager.removed) == 2 * iterations


def test_loop_tick_and_next_step_are_serialized():
    harness = MutationHarness(update_delay=0.02)
    tracker = harness.tracker
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def next_step_worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            for _ in range(2):
                harness.next_step()
        except BaseException as exc:  # pragma: no cover - failure path
            with errors_lock:
                errors.append(exc)

    harness.start()
    try:
        _wait_for(lambda: tracker.entries >= 1)

        threads = [
            threading.Thread(target=next_step_worker, daemon=True) for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15.0)

        assert not any(thread.is_alive() for thread in threads), "worker deadlocked"
    finally:
        harness.stop()

    assert errors == [], f"workers raised: {errors!r}"
    assert tracker.entries > 0
    assert tracker.max_active == 1, (
        f"loop and next_step overlapped: max concurrency {tracker.max_active}"
    )
    assert tracker.overlaps == 0


class _SyncOnlyHarness(TopologySyncMixin):
    """Exercise the guard inside update_simulation itself.

    Inherits only TopologySyncMixin so the lazily created lock fallback is
    covered, and uses no real emulator/link operations.
    """

    def __init__(self, delay: float = 0.02) -> None:
        self.simulation_manager = object()
        self.current_time_step = 0
        self.links: dict = {}
        self.tracker = SectionTracker()
        self.delay = delay

    def _sync_node_positions(self) -> None:
        with self.tracker.section("positions"):
            time.sleep(self.delay)

    def _sync_links(self) -> dict:
        with self.tracker.section("links"):
            time.sleep(self.delay)
        return {"links_to_add": [], "links_to_update": [], "links_to_remove": []}

    def bulk_link_operations(self, **kwargs) -> dict:
        return {
            "added_count": 0,
            "updated_count": 0,
            "links_to_add": [],
            "add_time_total": 0.0,
            "add_time_wall": 0.0,
            "add_time_per_link": 0.0,
            "update_time_total": 0.0,
            "update_time_wall": 0.0,
            "update_time_per_link": 0.0,
            "deleted_count": 0,
            "delete_time_total": 0.0,
            "delete_time_wall": 0.0,
            "delete_time_per_link": 0.0,
        }

    def _perform_local_link_operations(self, **kwargs) -> None:
        pass

    def _sync_interfaces_and_links(self) -> None:
        pass


def test_update_simulation_serializes_direct_callers():
    harness = _SyncOnlyHarness()
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait(timeout=5.0)
            for _ in range(2):
                harness.update_simulation(advance_timestep=False)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)

    assert not any(thread.is_alive() for thread in threads), "worker deadlocked"
    assert errors == [], f"workers raised: {errors!r}"
    assert harness.tracker.entries == 4 * 2 * 2  # positions + links per call
    assert harness.tracker.max_active == 1
    assert harness.tracker.overlaps == 0
    assert isinstance(harness.__dict__.get("_simulation_lock"), type(threading.RLock()))
