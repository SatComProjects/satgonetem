"""
Base routing daemon interface.
"""
from __future__ import annotations

import abc
from typing import (
    TYPE_CHECKING,
    Any,
    List,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


@runtime_checkable
class RoutingContext(Protocol):
    """Narrow topology surface needed by routing daemons.

    TopologyManager satisfies this structurally; daemons must not depend on
    the concrete services class.
    """

    satellites: Mapping[int, "Node"]
    ground_stations: Mapping[int, "Node"]
    links: Mapping[Any, "Link"]
    status: bool
    use_file_routes: bool
    project_name: str
    current_time_step: int

    def get_satellites(self) -> List["Node"]: ...

    def get_ground_stations(self) -> List["Node"]: ...

    def get_current_graph(self) -> Any: ...

    def _get_interface_to_peer(self, node: "Node", peer_id: int) -> Any: ...


class RoutingDaemon(abc.ABC):
    """Abstract base class for routing daemons.

    All routing strategies (static, OSPF, ISIS, SR-MPLS, etc.) must implement
    this interface so the topology manager can drive them uniformly.
    """

    def __init__(self, topology: RoutingContext) -> None:
        """
        Args:
            topology: The topology context that owns this daemon.
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
