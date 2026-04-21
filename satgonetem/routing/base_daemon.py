"""
Base routing daemon interface.
"""
from __future__ import annotations

import abc
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node
    from satgonetem.services.topology_satcom import TopologyManager


class RoutingDaemon(abc.ABC):
    """Abstract base class for routing daemons.

    All routing strategies (static, OSPF, ISIS, SR-MPLS, etc.) must implement
    this interface so the topology manager can drive them uniformly.
    """

    def __init__(self, topology: "TopologyManager") -> None:
        """
        Args:
            topology: The TopologyManager instance that owns this daemon.
        """
        self.topology = topology

    @abc.abstractmethod
    def init(self, max_workers: int = 4) -> bool:
        """Initialize routing on all nodes.

        Args:
            max_workers: Maximum number of worker threads for parallel operations.

        Returns:
            True if initialization succeeded, False otherwise.
        """

    @abc.abstractmethod
    def update(self, new_links: "List[Link]", max_workers: int = 4) -> None:
        """Update routing after topology changes.

        Called whenever links are added or removed from the topology.

        Args:
            new_links: List of newly added links.
            max_workers: Maximum number of worker threads for parallel operations.
        """

    @abc.abstractmethod
    def remove(self, node: "Optional[Node]" = None, max_workers: int = 4) -> None:
        """Remove all installed routes.

        Args:
            node: If provided, remove routes only from this node. If None,
                remove routes from all nodes in the topology.
            max_workers: Maximum number of worker threads for parallel operations.
        """
