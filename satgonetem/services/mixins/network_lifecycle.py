"""NetworkLifecycleMixin for TopologyManager."""

from __future__ import annotations

import time
import warnings
from satgonetem.utils.constants import MAX_WORKERS
from satgonetem.utils.utils import time_
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class NetworkLifecycleMixin:
    """NetworkLifecycle functionality."""

    def start_gonetem(self) -> float | None:
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

        container_time, link_time = launcher.start_containers(launch_nodes, MAX_WORKERS)
        launcher.wire_links(launch_links, MAX_WORKERS)

        if hil is not None:
            hil.wire_links(hil_links)

        self.launcher = launcher
        self.set_status(True)
        self.set_gonetem_status(True)
        self.start_time_ = time.time()

        return (container_time, link_time)

    def stop_gonetem(self) -> float:
        """Stop GoNetEm and clean up resources.

        Gracefully stops any active background activity (simulation loop,
        routing, tcpdump, traffic flows) before tearing down the network.

        Returns:
            float: Time taken to stop GoNetEm
        """
        tic = time.perf_counter()

        # Stop simulation loop if running
        if self.get_running() or (
            hasattr(self, "_sim_thread")
            and self._sim_thread is not None
            and self._sim_thread.is_alive()
        ):
            self.stop()

        # Wait for active traffic flows to finish
        if hasattr(self, "_active_flows"):
            for flow in self._active_flows:
                status = getattr(flow, "status", lambda: None)()
                if status is not None and getattr(status, "name", str(status)) in (
                    "RUNNING",
                    "PENDING",
                ):
                    thread = getattr(flow, "_thread", None)
                    if thread is not None and thread.is_alive():
                        thread.join()
            self._active_flows.clear()

        self.set_status(False)

        if self.hil_manager is not None:
            self.hil_manager.teardown_all()

        launcher = getattr(self, "launcher", None)

        if launcher is not None:
            launcher.close_project()

        self.set_gonetem_status(False)
        self.set_routing_initiated(False)
        self.routing_method = None

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
        self.set_status(False)

        if self.hil_manager is not None:
            self.hil_manager.teardown_all()

        launcher = getattr(self, "launcher", None)

        if launcher is not None:
            launcher.close_project()

        self.set_gonetem_status(False)
        self.set_routing_initiated(False)
        self.routing_method = None

        return time.perf_counter() - tic

    def set_ip_addresses(self) -> float:
        """Set IPv4 addresses for all interfaces in the topology."""
        tic: float = time.perf_counter()
        self.set_ipv4s_for_all_nodes(set_lo=True, max_workers=MAX_WORKERS)

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
