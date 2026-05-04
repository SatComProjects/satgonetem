"""NetworkLifecycleMixin for TopologyManager."""

from __future__ import annotations

import time
import warnings
from satgonetem.utils.constants import MAX_WORKERS
from satgonetem.utils.utils import time_
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.services.topology_satcom import TopologyManager


class NetworkLifecycleMixin:
    """NetworkLifecycle functionality."""

    @time_
    def start_gonetem(self) -> float | None:
        """Start GoNetEm by launching containers and wiring links.

        Returns:
            float: Time taken to start GoNetEm
        """
        tic = time.perf_counter()
        if self.get_gonetem_status():
            return

        if self.network_launcher.upper() == "GONETEM":
            from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher

            direct_launcher = GoNetEmLauncher(
                topology_manager=self,
                server_address=self.gonetem_server,
                project_name=self.project_name,
                isl_capacity_kbps=self.isl_link_capacity,
                gnd_capacity_kbps=self.gnd_link_capacity,
            )
        else:
            from satgonetem.launchers.direct_launcher import DirectLauncher

            direct_launcher = DirectLauncher(
                project_name=self.project_name,
                isl_capacity_kbps=self.isl_link_capacity,
                gnd_capacity_kbps=self.gnd_link_capacity,
                satellite_image=self.satellite_image,
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

        container_time, link_time = direct_launcher.start_containers(
            launch_nodes, MAX_WORKERS
        )  # If using gonetem, otherwise simply none
        direct_launcher.wire_links(launch_links, MAX_WORKERS)

        if hil is not None:
            hil.wire_links(hil_links)

        self.direct_launcher = direct_launcher
        self.set_status(True)
        self.set_gonetem_status(True)
        self.start_time_ = time.time()

        return (container_time, link_time)

    @time_
    def stop_gonetem(self) -> float:
        """Stop GoNetEm and clean up resources.

        Returns:
            float: Time taken to stop GoNetEm
        """
        tic = time.perf_counter()

        self.set_status(False)

        if self.hil_manager is not None:
            self.hil_manager.teardown_all()

        direct_launcher = getattr(self, "direct_launcher", None)

        if direct_launcher is not None:
            direct_launcher.close_project()

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

        direct_launcher = getattr(self, "direct_launcher", None)

        if direct_launcher is not None:
            direct_launcher.close_project()

        self.set_gonetem_status(False)
        self.set_routing_initiated(False)
        self.routing_method = None

        return time.perf_counter() - tic

    @time_
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
