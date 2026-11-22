"""SR-MPLS label allocation and path planning.

Extracted from the daemon so label management can be tested and evolved
without changing route-installation code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

MPLS_LABEL_MIN = 16
MPLS_LABEL_MAX = 1048575
MPLS_LABEL_IMPLICIT_NULL = 3


@dataclass
class NodeSID:
    """Node Segment Identifier for SR-MPLS.

    Each node gets a globally unique label (Node SID) that means
    "forward this packet toward me using shortest path".

    Attributes:
        node_id: The node's unique identifier.
        label: The MPLS label assigned as the Node SID.
        node_name: Human-readable name for logging.
    """

    node_id: int
    label: int
    node_name: str = ""

    def __hash__(self) -> int:
        return hash(self.node_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NodeSID):
            return NotImplemented
        return self.node_id == other.node_id


class SegmentRoutingManager:
    """SR-MPLS label manager: allocates Node SIDs and resolves label stacks.

    SR-MPLS uses source routing where the ingress node encodes the complete
    path as a stack of labels. Transit nodes pop their own Node SID and
    forward on the next label.

    Attributes:
        node_sid_base: Base label value for Node SIDs.
        node_sids: Mapping of node_id to NodeSID.
        custom_paths: Custom path overrides for specific ground-station pairs.
    """

    def __init__(self, node_sid_base: int = 16000, adj_sid_base: int = 100000) -> None:
        """Initialize the Segment Routing manager.

        Args:
            node_sid_base: Starting label for Node SIDs. Node SID = base + node_id.
            adj_sid_base: Starting label for Adjacency SIDs (reserved for future use).
        """
        if node_sid_base < MPLS_LABEL_MIN:
            node_sid_base = MPLS_LABEL_MIN

        self.node_sid_base = node_sid_base
        self.adj_sid_base = adj_sid_base
        self.node_sids: dict[int, NodeSID] = {}
        self.reverse_sid_map: dict[int, int] = {}
        self.custom_paths: dict[tuple[int, int], list[int]] = {}
        self._label_stack_cache: dict[
            tuple[int, int, bool, tuple[int, ...]], list[int]
        ] = {}

        logging.info(
            f"SegmentRoutingManager initialized with Node SID base {node_sid_base}"
        )

    def allocate_node_sid(self, node_id: int, node_name: str = "") -> NodeSID:
        """Allocate a stable Node SID for a node.

        The label is deterministic: node_sid_base + node_id.

        Args:
            node_id: The node's unique identifier.
            node_name: Human-readable name for logging.

        Returns:
            NodeSID object containing the allocated label.

        Raises:
            RuntimeError: If the computed label exceeds MPLS_LABEL_MAX.
        """
        if node_id in self.node_sids:
            return self.node_sids[node_id]

        label = self.node_sid_base + node_id
        if label > MPLS_LABEL_MAX:
            raise RuntimeError(
                f"Node SID {label} exceeds maximum MPLS label {MPLS_LABEL_MAX}"
            )

        sid = NodeSID(node_id=node_id, label=label, node_name=node_name)
        self.node_sids[node_id] = sid
        self.reverse_sid_map[label] = node_id

        logging.debug(f"Allocated Node SID {label} for node {node_name or node_id}")
        return sid

    def get_node_sid(self, node_id: int) -> int | None:
        """Return the Node SID label for a node, or None if not allocated.

        Args:
            node_id: The node's identifier.

        Returns:
            The MPLS label, or None.
        """
        sid = self.node_sids.get(node_id)
        return sid.label if sid else None

    def get_node_for_sid(self, label: int) -> int | None:
        """Return the node ID for a given Node SID label, or None.

        Args:
            label: The Node SID label.

        Returns:
            The node ID, or None if not found.
        """
        return self.reverse_sid_map.get(label)

    def set_custom_path(self, source_id: int, dest_id: int, path: list[int]) -> None:
        """Store a custom path for a source-destination pair.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.
            path: Ordered list of node IDs from source to destination.

        Raises:
            ValueError: If path is too short or endpoints do not match.
        """
        if len(path) < 2:
            raise ValueError("Path must have at least 2 nodes")
        if path[0] != source_id:
            raise ValueError("Path must start with source node")
        if path[-1] != dest_id:
            raise ValueError("Path must end with destination node")

        self.custom_paths[(source_id, dest_id)] = path
        self._invalidate_pair_cache(source_id, dest_id)

        logging.info(
            f"Set custom SR path {source_id} -> {dest_id}: "
            f"{' -> '.join(str(n) for n in path)}"
        )

    def get_custom_path(self, source_id: int, dest_id: int) -> list[int] | None:
        """Return the custom path for a pair, or None if using default routing.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.

        Returns:
            Custom path as list of node IDs, or None.
        """
        return self.custom_paths.get((source_id, dest_id))

    def clear_custom_path(self, source_id: int, dest_id: int) -> bool:
        """Remove a custom path, reverting to shortest-path routing.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.

        Returns:
            True if a custom path was removed, False if none existed.
        """
        self._invalidate_pair_cache(source_id, dest_id)
        return self.custom_paths.pop((source_id, dest_id), None) is not None

    def _invalidate_pair_cache(self, source_id: int, dest_id: int) -> None:
        """Drop every cached label stack for a source-destination pair.

        The cache is keyed by pair, PHP mode, and path, so removing only one
        key could leave a stale stack behind.  The key list is snapshotted
        first because other pair lookups can insert into the cache while the
        scan runs.

        Args:
            source_id: Source node ID.
            dest_id: Destination node ID.
        """
        stale_keys = [
            key
            for key in list(self._label_stack_cache)
            if key[0] == source_id and key[1] == dest_id
        ]
        for key in stale_keys:
            del self._label_stack_cache[key]

    def get_label_stack_for_path(
        self, path: list[int], use_php: bool = True
    ) -> list[int]:
        """Generate the label stack for a path.

        The source pushes Node SIDs for each transit hop. The stack is ordered
        so the first label to be processed is on top.

        Args:
            path: List of node IDs from source to destination.
            use_php: If True, omit the destination's SID (Penultimate Hop Popping).

        Returns:
            List of labels to push, bottom to top.

        Raises:
            ValueError: If any node in the path has no allocated Node SID.
        """
        if len(path) < 2:
            return []

        end_idx = -1 if use_php else len(path)
        segment_nodes = path[1:end_idx]

        labels = []
        for node_id in segment_nodes:
            sid = self.node_sids.get(node_id)
            if sid is None:
                raise ValueError(f"Node {node_id} does not have a Node SID allocated")
            labels.append(sid.label)

        return labels

    def get_sr_route_for_destination(
        self,
        source_id: int,
        dest_id: int,
        default_path: list[int] | None = None,
        use_php: bool = True,
    ) -> tuple[list[int], list[int]]:
        """Return the path and label stack for a destination.

        Uses the custom path when set, otherwise falls back to default_path.

        Args:
            source_id: Source node ID.
            dest_id: Destination node ID.
            default_path: Shortest path to use when no custom path exists.
            use_php: Use Penultimate Hop Popping.

        Returns:
            Tuple of (path, label_stack). Both are empty lists if no path exists.
        """
        custom = self.custom_paths.get((source_id, dest_id))
        path = custom if custom else default_path

        if not path or len(path) < 2:
            return [], []

        cache_key = (source_id, dest_id, use_php, tuple(path))
        if custom is None and cache_key in self._label_stack_cache:
            return path, self._label_stack_cache[cache_key]

        labels = self.get_label_stack_for_path(path, use_php=use_php)

        if custom is None:
            self._label_stack_cache[cache_key] = labels

        return path, labels

    def get_all_node_sids(self) -> list[NodeSID]:
        """Return all allocated Node SIDs.

        Returns:
            List of NodeSID objects.
        """
        return list(self.node_sids.values())

    def get_statistics(self) -> dict[str, int]:
        """Return SR manager statistics.

        Returns:
            Dict with node_sid_base, node_sids_allocated, custom_paths,
            and cached_stacks.
        """
        return {
            "node_sid_base": self.node_sid_base,
            "node_sids_allocated": len(self.node_sids),
            "custom_paths": len(self.custom_paths),
            "cached_stacks": len(self._label_stack_cache),
        }

    def clear_cache(self) -> None:
        """Clear the label stack cache."""
        self._label_stack_cache.clear()

    def reset(self) -> None:
        """Reset all SR state."""
        self.node_sids.clear()
        self.reverse_sid_map.clear()
        self.custom_paths.clear()
        self._label_stack_cache.clear()
        logging.info("SegmentRoutingManager reset")

    def __str__(self) -> str:
        stats = self.get_statistics()
        return (
            f"SegmentRoutingManager: {stats['node_sids_allocated']} Node SIDs, "
            f"{stats['custom_paths']} custom paths"
        )
