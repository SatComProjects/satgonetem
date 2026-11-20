"""RoutingManagerMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
import networkx as nx
from satgonetem.models.ground_station import GroundStation
from satgonetem.models.satellite import Satellite
from satgonetem.routing.ospf_daemon import OSPFDaemon
from satgonetem.routing.static_daemon import StaticRoutingDaemon
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.isis_sr_bird_daemon import ISISBirdSRDaemon
from satgonetem.utils.constants import MAX_WORKERS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.node import Node
    from typing import List


class RoutingManagerMixin:
    """RoutingManager functionality."""

    def get_path_between_nodes(self, source: Node, target: Node) -> List[Node]:
        """
        Get the path between two nodes using the specified preference.
        """
        graph = self.get_current_graph()

        if type(source) not in [GroundStation, Satellite]:
            raise TypeError("Source must be either a GroundStation or a Satellite")
        try:
            path_ids = nx.shortest_path(
                graph, source=source.id, target=target.id, weight="weight"
            )
            path_nodes = []

            for node_id in path_ids:
                # Check if it's a satellite
                if node_id in self.satellites:
                    path_nodes.append(self.satellites[node_id])
                # Check if it's a ground station
                else:
                    # Find the ground station by ID
                    for gs in self.ground_stations.values():
                        if gs.id == node_id:
                            path_nodes.append(gs)
                            break

            return path_nodes
        except nx.NetworkXNoPath:
            logging.warning(f"No path found between {source.id} and {target.id}")
            return []

    from satgonetem.utils.utils import time_

    def init_routing(
        self, max_workers: int = MAX_WORKERS, routing_method: str = ""
    ) -> float:
        """
        Initialize routing based on the configured routing method.

        Args:
            max_workers: Maximum number of worker threads to use for parallel operations.
            routing_method: Optional routing method to initialize for this call
                only. It does not change self.routing.
        Returns:
            float: Elapsed initialization time, or -1.0 if initialization failed.
        """
        tic = time.perf_counter()
        effective_method = routing_method or self.routing

        self.set_routing_initiated(False)

        # Tear down any previously installed daemon before replacing it. A
        # teardown failure must not prevent the new daemon from being set up.
        existing_daemon = getattr(self, "routing_daemon", None)
        if existing_daemon is not None:
            try:
                existing_daemon.remove(max_workers=max_workers)
            except Exception:
                logging.exception(
                    "Failed to tear down existing routing daemon during re-initialization"
                )
            finally:
                self.routing_daemon = None

        if effective_method == "static":
            daemon = StaticRoutingDaemon(self)
            failure_message = "Static IP routing initialization failed"
        elif effective_method == "dynamic-ospf":
            daemon = OSPFDaemon(self)
            failure_message = "Dynamic OSPF routing initialization failed"
        elif effective_method == "dynamic-isis":
            daemon = ISISBirdSRDaemon(self)
            failure_message = "Dynamic IS-IS SR (Bird) routing initialization failed"
        elif effective_method == "sr-mpls":
            daemon = SRMPLSDaemon(self)
            failure_message = "SR-MPLS routing initialization failed"
        elif effective_method in self._daemon_registry:
            daemon = self._daemon_registry[effective_method](self)
            failure_message = (
                f"Custom routing daemon '{effective_method}' initialization failed"
            )
        else:
            logging.warning(
                f"Unknown routing method '{effective_method}', cannot initialize routing"
            )
            return -1.0

        try:
            initialized = daemon.init(max_workers=max_workers)
        except Exception:
            logging.exception(failure_message)
            initialized = False
        else:
            if not initialized:
                logging.warning(failure_message)

        if not initialized:
            # Do not leave a half-initialized daemon installed.
            try:
                daemon.remove(max_workers=max_workers)
            except Exception:
                logging.exception(
                    "Failed to clean up routing daemon after initialization failure"
                )
            self.routing_daemon = None
            self.set_routing_initiated(False)
            return -1.0

        self.routing_daemon = daemon
        self.set_routing_initiated(True)
        return time.perf_counter() - tic

    def delete_routing(self) -> float:
        """Delete all installed static IP routes."""
        if not self.get_routing_initiated() or self.routing_daemon is None:
            logging.warning("Routing is not initiated, cannot delete routes")
            return -1.0
        tic: float = time.perf_counter()

        self.routing_daemon.remove(max_workers=MAX_WORKERS)
        self.set_routing_initiated(False)
        self.routing_method = None
        self.routing_daemon = None

        return time.perf_counter() - tic

    def get_allowed_routing_methods(self) -> List[str]:
        """Return a list of allowed routing methods."""
        return self.allowed_routing_methods

    def get_routing_initiated(self) -> bool:
        """Return whether routing has been initialised via init_routing()."""
        return self.routing_initiated

    def set_routing_initiated(self, value: bool) -> None:
        """Set the routing-initiated flag.

        Args:
            value: True after init_routing() succeeds, False after routing is torn down.
        """
        self.routing_initiated = value
