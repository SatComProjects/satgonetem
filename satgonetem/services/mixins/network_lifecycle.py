"""NetworkLifecycleMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
import warnings
from satgonetem.utils.constants import MAX_WORKERS
from satgonetem.utils.utils import time_
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# Maximum time to wait for one traffic flow thread during shutdown.
_FLOW_STOP_TIMEOUT_SEC = 5.0


class NetworkLifecycleMixin:
    """NetworkLifecycle functionality."""

    def start_gonetem(self, controller: bool = False) -> float | None:
        """Start GoNetEm by launching containers and wiring links.

        Returns:
            float: Time taken to start GoNetEm
        """
        tic = time.perf_counter()
        if self.get_gonetem_status():
            return

        from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher

        launcher = GoNetEmLauncher(
            topology_manager=self,
            server_address=self.gonetem_server,
            project_name=self.project_name,
            isl_capacity_kbps=self.isl_link_capacity,
            gnd_capacity_kbps=self.gnd_link_capacity,
            ground_object_capacity_kbps=getattr(
                self, "ground_object_link_capacity", self.gnd_link_capacity
            ),
        )
        # Assign before startup so a failed launch can still be torn down.
        self.launcher = launcher

        all_nodes = list(self.satellites.values()) + list(self.ground_stations.values())
        active_links = [lnk for lnk in self.links.values() if lnk.is_active]

        hil = self.hil_manager
        if hil is not None:
            launch_nodes = [n for n in all_nodes if not hil.is_hil_node(n.name)]
            launch_links = [lnk for lnk in active_links if not hil.is_hil_link(lnk)]
            hil_links = [lnk for lnk in active_links if hil.is_hil_link(lnk)]
        else:
            launch_nodes = all_nodes
            launch_links = active_links
            hil_links = []

        try:
            container_time, link_time = launcher.start_containers(
                launch_nodes,
                MAX_WORKERS,
                controller=controller,
                links=launch_links,
            )
            launcher.wire_links(launch_links, MAX_WORKERS)

            if hil is not None:
                hil.wire_links(hil_links)
        except Exception:
            # Startup failed after the project may have been opened. Close it
            # best-effort and reset state so a retry does not leak a project.
            try:
                launcher.force_close_project()
            except Exception as cleanup_exc:
                logging.error(
                    "Failed to force-close GoNetEm project after startup "
                    "failure: %s",
                    cleanup_exc,
                )
            self.launcher = None
            self.set_status(False)
            self.set_gonetem_status(False)
            self.set_routing_initiated(False)
            raise

        self.set_status(True)
        self.set_gonetem_status(True)
        self.start_time_ = time.time()

        return (container_time, link_time)

    def _stop_simulation_loop(self) -> None:
        """Stop the background simulation loop if it is running."""
        if self.get_running() or (
            hasattr(self, "_sim_thread")
            and self._sim_thread is not None
            and self._sim_thread.is_alive()
        ):
            self.stop()

    def _stop_active_flows(self) -> None:
        """Stop active traffic flows with a bounded wait.

        A single stuck command must not prevent container/project teardown.
        """
        flows = list(getattr(self, "_active_flows", []))
        try:
            for flow in flows:
                status = getattr(flow, "status", lambda: None)()
                if status is None or getattr(status, "name", str(status)) not in (
                    "RUNNING",
                    "PENDING",
                ):
                    continue

                stop = getattr(flow, "stop", None)
                if callable(stop):
                    try:
                        stop()
                    except Exception as exc:
                        logging.warning(
                            "Failed to stop traffic flow during shutdown: %s", exc
                        )

                thread = getattr(flow, "_thread", None)
                if thread is not None and thread.is_alive():
                    thread.join(timeout=_FLOW_STOP_TIMEOUT_SEC)
                    if thread.is_alive():
                        logging.warning(
                            "Traffic flow thread did not stop within %.1fs; "
                            "continuing shutdown",
                            _FLOW_STOP_TIMEOUT_SEC,
                        )
        finally:
            if hasattr(self, "_active_flows"):
                self._active_flows.clear()

    def stop_gonetem(self) -> float:
        """Stop GoNetEm and clean up resources.

        Gracefully stops any active background activity (simulation loop,
        routing, tcpdump, traffic flows) before tearing down the network.

        Returns:
            float: Time taken to stop GoNetEm
        """
        tic = time.perf_counter()

        self._stop_simulation_loop()
        self._stop_active_flows()

        self.set_status(False)

        launcher = getattr(self, "launcher", None)
        teardown_error: Exception | None = None

        try:
            if self.hil_manager is not None:
                self.hil_manager.teardown_all()
        except Exception as exc:
            teardown_error = exc
            logging.error("HIL teardown failed during stop: %s", exc)

        try:
            if launcher is not None:
                launcher.close_project()
        finally:
            # A failed stop must not leave the manager in a state where
            # start_gonetem() silently refuses to run (CORE-07).
            self.launcher = None
            self.set_gonetem_status(False)
            self.set_routing_initiated(False)
            self.routing_method = None

        if teardown_error is not None:
            raise teardown_error

        return time.perf_counter() - tic

    def force_stop_gonetem(self) -> float:
        """Force stop GoNetEm without graceful cleanup.

        This method can be used in scenarios where the normal stop_gonetem process fails
        or when a quick reset is needed. It will attempt to kill all containers and clean
        up resources without waiting for graceful shutdown.

        Returns:
            float: Time taken to force stop GoNetEm
        """
        warnings.warn(
            "force_stop_gonetem does not clean up properly; "
            "prefer stop_gonetem() for a clean shutdown.",
            ResourceWarning,
            stacklevel=2,
        )
        tic = time.perf_counter()

        teardown_error: Exception | None = None
        try:
            # Stop the simulation loop and traffic flows before closing the
            # project so no background thread mutates a closed topology.
            try:
                self._stop_simulation_loop()
            except Exception as exc:
                logging.error(
                    "Failed to stop simulation loop during force stop: %s", exc
                )

            self.set_status(False)

            try:
                self._stop_active_flows()
            except Exception as exc:
                logging.error(
                    "Failed to stop traffic flows during force stop: %s", exc
                )

            if self.hil_manager is not None:
                try:
                    self.hil_manager.teardown_all()
                except Exception as exc:
                    teardown_error = exc
                    logging.error("HIL teardown failed during force stop: %s", exc)

            launcher = getattr(self, "launcher", None)
            if launcher is not None:
                try:
                    launcher.force_close_project()
                except Exception as exc:
                    logging.error(
                        "Failed to force-close GoNetEm project during force stop: %s",
                        exc,
                    )
        finally:
            # A failed force stop must not leave stale state behind.
            self.launcher = None
            self.set_status(False)
            self.set_gonetem_status(False)
            self.set_routing_initiated(False)
            self.routing_method = None

        if teardown_error is not None:
            raise teardown_error

        return time.perf_counter() - tic

    def set_ip_addresses(
        self, satellites: bool = True, ground_stations: bool = True
    ) -> float:
        """Set IPv4 addresses for all interfaces in the topology.

        Args:
            satellites:  Optinal flag to assign addresses to satellites (defaults to True)
            ground_stations: Optional flag to assign addresses to ground stations (defaults to True)

        """
        tic: float = time.perf_counter()
        self.set_ipv4s_for_all_nodes(
            set_lo=True,
            max_workers=MAX_WORKERS,
            satellites=satellites,
            ground_stations=ground_stations,
        )

        return time.perf_counter() - tic

    def fast_start(self, routing_method: str = "static") -> None:
        """Start GoNetEm with a fast startup sequence for rapid testing iterations.

        This method performs a streamlined startup process that skips some of the
        more time-consuming steps like waiting for containers to be fully ready or
        launching tcpdump. It is intended for use in development and testing scenarios
        where quick feedback is more valuable than a fully initialized environment.

        Args:
            routing_method: Optional routing method to initialize (default: 'static').
                            Must be one of the allowed routing methods.
        """
        self.start_gonetem()
        self.set_ip_addresses()
        self.init_routing(routing_method=routing_method)
