"""SimulationLoopMixin for TopologyManager."""

from __future__ import annotations

import logging
import threading
import time
from satgonetem.utils.constants import MAX_WORKERS
from satgonetem.utils.utils import time_

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.services.topology_satcom import TopologyManager
    from datetime import datetime


class SimulationLoopMixin:
    """SimulationLoop functionality."""

    def start(self) -> None:
        """Start the simulation loop in a background thread.

        Raises:
            RuntimeError: If no project is loaded.
        """
        if self.get_running():
            return
        if not self.project_name:
            raise RuntimeError("Open or create a project first.")
        self._stop_evt.clear()
        self.set_running(True)
        self._sim_thread = threading.Thread(target=self._loop, daemon=True)
        self._sim_thread.start()

    def stop(self) -> None:
        """Stop the simulation loop and join the background thread."""
        self._stop_evt.set()
        self.set_running(False)
        if self._sim_thread and self._sim_thread.is_alive():
            self._sim_thread.join()

    @time_
    def next_step(self) -> float:
        """Advance the simulation by one step.

        Delegates directly to update_simulation.
        """
        tic = time.perf_counter()
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

    def _loop(self) -> None:
        """Simulation loop body (runs in a background thread).

        Calls update_simulation once per tick. The tick period is
        update_time * update_factor seconds, polled in 0.2-second slices
        so stop() is noticed promptly.
        """
        try:
            while not self._stop_evt.is_set():
                self.update_simulation()
                delay = max(0.01, self.update_time * self.update_factor)
                self._stop_evt.wait(timeout=delay)
        finally:
            self.set_running(False)

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

        Args:
            new_time: Target simulation datetime.

        Returns:
            None

        Raises:
            ValueError: If simulation_manager has not been initialised.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

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
