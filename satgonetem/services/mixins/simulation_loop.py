"""SimulationLoopMixin for TopologyManager.

Threading policy for simulation mutations
-----------------------------------------
``TopologyManager`` owns one reentrant :class:`threading.RLock`
(``_simulation_lock``) that serializes every simulation topology mutation:
the background loop tick and the API entry points (:meth:`next_step`,
:meth:`move_to_time`, :meth:`simulate_link_failure`). API mutations are not
queued or rejected; they block until the in-flight tick (or other mutation)
finishes and then run to completion atomically. The lock is reentrant because
the API methods nest :meth:`update_simulation`, and it is deliberately
separate from ``_lifecycle_lock``, which ``stop()`` holds while joining the
loop thread and therefore must not be re-acquired by that thread.
"""

from __future__ import annotations

import logging
import threading
import time
from satgonetem.utils.constants import MAX_WORKERS
from satgonetem.utils.utils import time_

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from datetime import datetime


def ensure_simulation_lock(owner: object) -> threading.RLock:
    """Return ``owner``'s simulation lock, creating one lazily when absent.

    ``TopologyManager`` installs the lock in ``__init__``. Lightweight
    harnesses that compose the mixins without that initializer still get a
    per-instance lock via ``__dict__.setdefault``.
    """
    lock = getattr(owner, "_simulation_lock", None)
    if lock is None:
        lock = owner.__dict__.setdefault("_simulation_lock", threading.RLock())
    return lock


class SimulationLoopMixin:
    """SimulationLoop functionality."""

    def _get_simulation_lock(self) -> threading.RLock:
        """Return the per-instance lock serializing simulation mutations.

        API mutations block until the in-flight loop tick finishes; nested
        calls re-acquire the same reentrant lock safely.
        """
        return ensure_simulation_lock(self)

    def start(self) -> None:
        """Start the simulation loop in a background thread.

        Serialized with :meth:`stop` so a concurrent stop cannot clear the
        event of a freshly started loop. Each run gets its own event.

        Raises:
            RuntimeError: If no project is loaded.
        """
        with self._lifecycle_lock:
            if self.get_running():
                return
            if not self.project_name:
                raise RuntimeError("Open or create a project first.")

            previous = self._sim_thread
            if previous is not None and previous.is_alive():
                self._stop_evt.set()
                previous.join()

            self._stop_evt = threading.Event()
            self.set_running(True)
            self._sim_thread = threading.Thread(
                target=self._loop, args=(self._stop_evt,), daemon=True
            )
            self._sim_thread.start()

    def stop(self) -> None:
        """Stop the simulation loop and join the background thread."""
        with self._lifecycle_lock:
            self._stop_evt.set()
            self.set_running(False)
            thread = self._sim_thread
            if thread is not None and thread.is_alive():
                thread.join()

    @time_
    def next_step(self) -> float:
        """Advance the simulation by one step.

        Delegates directly to update_simulation while holding the simulation
        lock, so it cannot interleave with a loop tick or another API mutation.
        The call blocks until any in-flight tick finishes.
        """
        tic = time.perf_counter()
        with self._get_simulation_lock():
            self.update_simulation()
        total = time.perf_counter() - tic

        return total if total > 0 else 0.00

    def speed_up(self) -> None:
        """Reduce the update factor to speed up simulation playback.

        Clamps to a minimum of 0.01.
        """
        self.update_factor = max(0.01, round(self.update_factor * 0.9, 2))

    def speed_down(self) -> None:
        """Increase the update factor to slow down simulation playback.

        Clamps to a maximum of 10.0.
        """
        self.update_factor = min(10.0, round(self.update_factor * 1.1, 2))

    def set_update_time(self, seconds: int) -> None:
        """Set the simulation tick interval in seconds.

        Args:
            seconds: Desired interval; clamped to a minimum of 1.
        """
        self.update_time = max(1, int(seconds))

    def _loop(self, stop_evt: threading.Event) -> None:
        """Simulation loop body (runs in a background thread).

        Calls update_simulation once per tick. The tick period is
        update_time * update_factor seconds, polled in 0.2-second slices
        so stop() is noticed promptly. Each tick holds the simulation lock, so
        an API mutation blocks until the tick finishes (and vice versa).

        A failing tick is logged with its traceback and recorded in
        ``last_simulation_error`` (readable via
        :meth:`get_last_simulation_error`); the loop keeps running so
        transient failures are retried on the next tick.

        Args:
            stop_evt: Event owned by this run; set by :meth:`stop`.
        """
        try:
            while not stop_evt.is_set():
                tic = time.perf_counter()
                try:
                    with self._get_simulation_lock():
                        self.update_simulation()
                except Exception as exc:
                    self.last_simulation_error = exc
                    logging.exception(
                        "Simulation loop tick failed; retrying on next tick"
                    )
                toc = time.perf_counter()
                elapsed = toc - tic
                delay = max(0.01, self.update_time * self.update_factor)
                to_wait = max(0.01, delay - elapsed)
                stop_evt.wait(timeout=to_wait)
        finally:
            self.set_running(False)

    def get_last_simulation_error(self) -> Optional[BaseException]:
        """Return the most recent exception raised by a loop tick.

        Returns ``None`` until a tick fails. The error is retained after later
        successful ticks so the cause of a transient failure stays diagnosable
        until the next failure replaces it. Lightweight harnesses that compose
        this mixin without ``TopologyManager.__init__`` get the attribute
        lazily here.
        """
        return self.__dict__.setdefault("last_simulation_error", None)

    def get_running(self) -> bool:
        """Return whether the simulation loop has been started via start()."""
        return self.running

    def set_running(self, value: bool) -> None:
        """Set the simulation loop running state.

        Args:
            value: True when start() has been called, False when stopped.
        """
        self.running = value

    def move_to_time(self, new_time: datetime) -> None:
        """
        Jump the simulation to an absolute time and rebuild routing.

        Delegates the time-manager advance to TopologyManager, then triggers the
        emulator-level routing rebuild that requires TopologyManager state.
        The whole operation holds the simulation lock, so the time advance,
        ``current_time_step`` update, and routing rebuild are atomic with
        respect to loop ticks and other API mutations. The call blocks until any
        in-flight tick finishes.

        Args:
            new_time: Target simulation datetime.

        Returns:
            None

        Raises:
            ValueError: If simulation_manager has not been initialised.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        with self._get_simulation_lock():
            self.simulation_manager.time_manager.set_time(new_time)
            self.simulation_manager.time_manager.execute_actions()
            self.current_time_step = int(
                (new_time - self.start_time).total_seconds() / self.update_time
            )
            self.rebuild_routing_for_current_timestep()

    def rebuild_routing_for_current_timestep(self):
        """
        Rebuild routing tables for the current time step.

        Delegates to the active routing daemon. For static routing, the daemon
        recomputes Dijkstra paths and applies incremental route changes to all
        containers. Dynamic (FRR) and SR-MPLS daemons handle their own updates.
        """
        if self.routing_daemon is not None and self.get_status():
            self.routing_daemon.update([], max_workers=MAX_WORKERS)

    def simulate_link_failure(self, links: Link | list[Link]) -> None:
        """
        Simulate a link failure by removing one or more links from the current graph.

        Each supplied ``Link`` must wrap a sat_com_model link object whose ``type``
        attribute is one of the supported link kinds. The corresponding removal
        method on ``self.simulation_manager`` is invoked for every link.

        The whole multi-link mutation and the follow-up topology update hold the
        simulation lock, so they are atomic with respect to loop ticks and other
        API mutations. The call blocks until any in-flight tick finishes.

        Args:
            links: A single ``Link`` instance or a list of ``Link`` instances to
                remove from the simulation manager.

        Raises:
            AttributeError: If a link has no ``type`` attribute or its type is not
                supported.
        """
        with self._get_simulation_lock():
            if not isinstance(links, list):
                links = [links]

            logging.info(
                "Simulating link failure for %d link(s) on project '%s'",
                len(links),
                getattr(self, "project_name", "unknown"),
            )

            valid_link_types = (
                "InterSatelliteLink",
                "GroundStationLink",
                "UserTerminalLink",
            )

            for link in links:
                satcom_link = getattr(link, "satcom_object", None)
                link_type = getattr(satcom_link, "type", None)
                source_name = getattr(getattr(link, "source", None), "name", "unknown")
                target_name = getattr(getattr(link, "target", None), "name", "unknown")

                if link_type not in valid_link_types:
                    logging.error(
                        "Cannot simulate failure for link %s -> %s: invalid type '%s'",
                        source_name,
                        target_name,
                        link_type,
                    )
                    raise AttributeError(
                        "Link has no type attribute or does not have a valid link type"
                    )

                logging.info(
                    "Removing %s between %s and %s",
                    link_type,
                    source_name,
                    target_name,
                )

                if link_type == "InterSatelliteLink":
                    self.simulation_manager.remove_inter_satellite_link(satcom_link)
                elif link_type == "GroundStationLink":
                    self.simulation_manager.remove_ground_station_link(satcom_link)
                elif link_type == "UserTerminalLink":
                    self.simulation_manager.remove_user_terminal_link(satcom_link)

            self.update_simulation(advance_timestep=False)

            logging.info(
                "Link failure simulation complete for project '%s' (%d link(s) processed)",
                getattr(self, "project_name", "unknown"),
                len(links),
            )