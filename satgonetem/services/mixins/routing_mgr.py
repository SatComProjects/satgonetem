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
            routing_method: Optional routing method to initialize (overrides self.routing if provided).
        Returns:
            bool: True if initialization is successful, False otherwise.
        """
        tic = time.perf_counter()
        if routing_method:
            self.routing = routing_method

        if self.routing == "static":
            self.routing_daemon = StaticRoutingDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("Static IP routing initialization failed")

                return -1.0
        elif self.routing == "dynamic-ospf":
            self.routing_daemon = OSPFDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("Dynamic OSPF routing initialization failed")
                return -1.0
        elif self.routing == "dynamic-isis":
            self.routing_daemon = ISISBirdSRDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("Dynamic IS-IS SR (Bird) routing initialization failed")
                return -1.0
        elif self.routing == "sr-mpls":
            self.routing_daemon = SRMPLSDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("SR-MPLS routing initialization failed")
                return -1.0
        elif self.routing in self._daemon_registry:
            self.routing_daemon = self._daemon_registry[self.routing](self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning(
                    f"Custom routing daemon '{self.routing}' initialization failed"
                )
                return -1.0
        else:
            logging.warning(
                f"Unknown routing method '{self.routing}', cannot initialize routing"
            )
            return -1.0

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
