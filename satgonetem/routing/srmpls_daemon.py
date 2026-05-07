"""SR-MPLS routing daemon.

Owns all Segment Routing MPLS state and logic. The topology manager holds no
SR-MPLS state; all label allocation, route building, and route application are
encapsulated here.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import networkx as nx

from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.utils.utils import time_
from satgonetem.models.mpls_entry import SRForwardEntry, SRNodeSIDEntry
from satgonetem.models.satellite import Satellite
from satgonetem.models.ground_station import GroundStation

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


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
        self.node_sids: Dict[int, NodeSID] = {}
        self.reverse_sid_map: Dict[int, int] = {}
        self.custom_paths: Dict[Tuple[int, int], List[int]] = {}
        self._label_stack_cache: Dict[Tuple[int, int], List[int]] = {}

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

    def get_node_sid(self, node_id: int) -> Optional[int]:
        """Return the Node SID label for a node, or None if not allocated.

        Args:
            node_id: The node's identifier.

        Returns:
            The MPLS label, or None.
        """
        sid = self.node_sids.get(node_id)
        return sid.label if sid else None

    def get_node_for_sid(self, label: int) -> Optional[int]:
        """Return the node ID for a given Node SID label, or None.

        Args:
            label: The Node SID label.

        Returns:
            The node ID, or None if not found.
        """
        return self.reverse_sid_map.get(label)

    def set_custom_path(self, source_id: int, dest_id: int, path: List[int]) -> None:
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
        self._label_stack_cache.pop((source_id, dest_id), None)

        logging.info(
            f"Set custom SR path {source_id} -> {dest_id}: "
            f"{' -> '.join(str(n) for n in path)}"
        )

    def get_custom_path(self, source_id: int, dest_id: int) -> Optional[List[int]]:
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
        self._label_stack_cache.pop((source_id, dest_id), None)
        return self.custom_paths.pop((source_id, dest_id), None) is not None

    def get_label_stack_for_path(
        self, path: List[int], use_php: bool = True
    ) -> List[int]:
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
        default_path: Optional[List[int]] = None,
        use_php: bool = True,
    ) -> Tuple[List[int], List[int]]:
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

        cache_key = (source_id, dest_id)
        if cache_key in self._label_stack_cache and custom is None:
            return path, self._label_stack_cache[cache_key]

        labels = self.get_label_stack_for_path(path, use_php=use_php)

        if custom is None:
            self._label_stack_cache[cache_key] = labels

        return path, labels

    def get_all_node_sids(self) -> List[NodeSID]:
        """Return all allocated Node SIDs.

        Returns:
            List of NodeSID objects.
        """
        return list(self.node_sids.values())

    def get_statistics(self) -> Dict[str, int]:
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


class SRMPLSDaemon(RoutingDaemon):
    """Segment Routing MPLS routing daemon.

    Owns the full SR-MPLS lifecycle: label allocation, route building, and
    forwarding-table installation on every node. The topology manager carries
    no SR-MPLS state; every piece of it lives here.

    SR-MPLS model used by this daemon:
    - Each node gets one stable Node SID (label = base + node_id).
    - Ground stations push a label stack encoding the complete path.
    - Satellites only need their own SID pop rule and per-neighbour forward
      rules, so satellite LFIB size is O(degree) rather than O(N).
    - Only ground-station routes change when the topology changes; satellite
      configuration is installed once at init and never touched again.
    """

    def __init__(self, topology: "TopologyManager") -> None:  # type: ignore[name-defined]
        """
        Args:
            topology: The TopologyManager instance that owns this daemon.
        """
        super().__init__(topology)
        self._label_manager: Optional[SegmentRoutingManager] = None
        self.sr_lfib: Dict[int, List] = {}
        self._sr_graph: Optional[Any] = None
        self.mpls_config: Optional[Any] = None
        self._prev_sr_gs_routes: Dict[int, List[str]] = {}
        self._prev_sr_sat_routes: Dict[int, List[str]] = {}
        self._prev_sr_node_sid_routes: Dict[int, List[str]] = {}

    @property
    def is_initialized(self) -> bool:
        """Return True after init() has been called successfully."""
        return self._label_manager is not None

    def get_node_sid(self, node_id: int) -> Optional[int]:
        """Return the Node SID label for a node, or None if not allocated.

        Args:
            node_id: The node identifier.

        Returns:
            The MPLS label assigned as this node's SID, or None.
        """
        if self._label_manager is None:
            return None
        return self._label_manager.get_node_sid(node_id)

    def init(self, max_workers: int = 128) -> bool:
        """Initialize SR-MPLS routing on all nodes.

        Sets MPLS configuration, allocates Node SIDs for every satellite and
        ground station, enables MPLS forwarding on all containers, and installs
        forwarding entries.

        Args:
            max_workers: Maximum number of parallel worker threads.

        Returns:
            True if initialization succeeded, False otherwise.
        """
        try:

            self._configure_mpls()

            self._init_sr_mpls()

            self._rebuild_sr_mpls_routing(init_flag=True, max_workers=max_workers)

            return True
        except Exception as e:
            logging.error(f"SR-MPLS initialization failed: {e}")
            return False

    def update(self, _new_links: "List[Link]", max_workers: int = 128) -> None:
        """Rebuild SR-MPLS ground-station routes after topology changes.

        Satellite Node SID rules are stable and never touched here. Only
        ground-station label-stack routes are recomputed.

        Args:
            _new_links: Unused. The topology graph is re-fetched from the
                topology manager on each rebuild.
            max_workers: Maximum number of parallel worker threads.
        """
        self._rebuild_sr_mpls_routing(init_flag=False, max_workers=max_workers)

    def remove(self, node: "Optional[Node]" = None, max_workers: int = 128) -> None:
        """Remove all SR-MPLS forwarding entries.

        Flushes the kernel MPLS label-forwarding table on every node (or on a
        single node if provided), then resets all in-memory SR state.

        Args:
            node: If provided, flush only this node. Otherwise flush all
                satellites and ground stations.
            max_workers: Maximum number of parallel worker threads.
        """
        if node is not None:
            self._flush_node(node)
        else:
            self._flush_all_nodes(max_workers)

        self.sr_lfib = {}
        self._sr_graph = None
        self._label_manager = None
        self._prev_sr_gs_routes.clear()
        self._prev_sr_sat_routes.clear()
        self._prev_sr_node_sid_routes.clear()

    def set_sr_custom_path(
        self, source_id: int, dest_id: int, path: List[int]
    ) -> Dict[str, Any]:
        """Set a custom SR-MPLS path for a source-destination pair.

        Validates the path, stores it in the label manager, and immediately
        re-applies routes on the source ground station.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.
            path: Ordered list of node IDs from source to destination.

        Returns:
            Dict with status, message, path, label_stack, and hop_count.

        Raises:
            RuntimeError: If SR-MPLS has not been initialized.
            ValueError: If the path is shorter than 2 nodes, does not start
                with source_id, does not end with dest_id, or contains an
                unknown node ID.
        """
        if self._label_manager is None:
            raise RuntimeError("SR-MPLS not initialized")

        if len(path) < 2:
            raise ValueError("Path must have at least 2 nodes")
        if path[0] != source_id:
            raise ValueError(f"Path must start with source {source_id}, got {path[0]}")
        if path[-1] != dest_id:
            raise ValueError(
                f"Path must end with destination {dest_id}, got {path[-1]}"
            )

        for node_id in path:
            if (
                node_id not in self.topology.satellites
                and node_id not in self.topology.ground_stations
            ):
                raise ValueError(f"Node {node_id} not found in topology")

        self._label_manager.set_custom_path(source_id, dest_id, path)

        if self.topology.status:
            self._update_sr_route_for_source(source_id)

        use_php = self.mpls_config.use_php if self.mpls_config else False
        label_stack = self._label_manager.get_label_stack_for_path(
            path, use_php=use_php
        )

        return {
            "status": "success",
            "message": f"Custom SR path set for {source_id} -> {dest_id}",
            "path": path,
            "label_stack": label_stack,
            "hop_count": len(path) - 1,
        }

    def clear_sr_custom_path(self, source_id: int, dest_id: int) -> Dict[str, Any]:
        """Remove a custom SR path and revert to shortest-path routing.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.

        Returns:
            Dict with status, message, and reverted_to_shortest.

        Raises:
            RuntimeError: If SR-MPLS has not been initialized.
        """
        if self._label_manager is None:
            raise RuntimeError("SR-MPLS not initialized")

        removed = self._label_manager.clear_custom_path(source_id, dest_id)

        if removed and self.topology.status:
            self._update_sr_route_for_source(source_id)

        return {
            "status": "success",
            "message": (
                f"Custom path {'removed' if removed else 'not found'} "
                f"for {source_id} -> {dest_id}"
            ),
            "reverted_to_shortest": removed,
        }

    def get_sr_statistics(self) -> Dict[str, Any]:
        """Return SR-MPLS statistics.

        Returns:
            Dict with enabled, node_sids_allocated, custom_paths, sr_routes,
            and ground_stations_with_routes.
        """
        if self._label_manager is None:
            return {"enabled": False}

        stats = self._label_manager.get_statistics()
        stats["enabled"] = True
        stats["sr_routes"] = sum(len(v) for v in self.sr_lfib.values())
        stats["ground_stations_with_routes"] = len(self.sr_lfib)
        return stats

    def list_sr_custom_paths(self) -> List[Dict[str, Any]]:
        """List all active custom SR paths.

        Returns:
            List of dicts with source_id, source_name, destination_id,
            destination_name, path, label_stack, and hop_count.
        """
        if self._label_manager is None:
            return []

        from satgonetem.models.ground_station import GroundStation

        use_php = self.mpls_config.use_php if self.mpls_config else False
        result = []
        for (src_id, dst_id), path in self._label_manager.custom_paths.items():
            label_stack = self._label_manager.get_label_stack_for_path(
                path, use_php=use_php
            )
            match self.topology.ground_stations.get(src_id):
                case GroundStation() as src_node:
                    src_name = src_node.name
                case None:
                    src_name = str(src_id)
                case _ as unexpected:
                    raise TypeError(
                        f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                    )
            match self.topology.ground_stations.get(dst_id):
                case GroundStation() as dst_node:
                    dst_name = dst_node.name
                case None:
                    dst_name = str(dst_id)
                case _ as unexpected:
                    raise TypeError(
                        f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                    )
            result.append(
                {
                    "source_id": src_id,
                    "source_name": src_name,
                    "destination_id": dst_id,
                    "destination_name": dst_name,
                    "path": path,
                    "label_stack": label_stack,
                    "hop_count": len(path) - 1,
                }
            )
        return result

    def _configure_mpls(self) -> None:
        """Set MPLS configuration on the daemon if not already present.

        Uses existing mpls_config when available; otherwise applies defaults
        matching the SR-MPLS standard label range with no PHP.
        """
        from satgonetem.models.mpls_entry import MPLSConfig

        if self.mpls_config is None:
            self.mpls_config = MPLSConfig(
                enabled=True,
                label_range_start=16,
                label_range_end=1048575,
                use_ldp=False,
                use_php=False,
                ttl=64,
                use_sr=True,
                sr_node_sid_base=16000,
            )

        self.mpls_config.enabled = True

    def _init_sr_mpls(self) -> None:
        """Allocate a stable Node SID for every satellite and ground station.

        The label is deterministic: node_sid_base + node_id. After this call
        is_initialized returns True.
        """
        sr_base = (
            getattr(self.mpls_config, "sr_node_sid_base", 16000)
            if self.mpls_config
            else 16000
        )
        self._label_manager = SegmentRoutingManager(node_sid_base=sr_base)

        for sat_id, sat in self.topology.satellites.items():
            self._label_manager.allocate_node_sid(sat_id, sat.name)

        for gs_id, gs in self.topology.ground_stations.items():
            self._label_manager.allocate_node_sid(gs_id, gs.name)

        logging.info(
            f"SR-MPLS initialized: {len(self._label_manager.node_sids)} Node SIDs allocated"
        )

    def _rebuild_sr_mpls_routing(
        self, init_flag: bool = True, max_workers: int = 4
    ) -> None:
        """Rebuild SR-MPLS routing for the current time step.

        Satellite Node SID rules are stable; only ground-station label-stack
        routes are recomputed each call.

        Args:
            init_flag: When True, also enables MPLS on all nodes before
                applying routes.
            max_workers: Maximum number of parallel worker threads.
        """
        if self._label_manager is None:
            logging.warning("SR-MPLS routing called but SR manager not initialized")
            return

        if init_flag:
            self._prev_sr_gs_routes.clear()
            self._prev_sr_sat_routes.clear()
            self._prev_sr_node_sid_routes.clear()

        self._label_manager.clear_cache()
        self._build_sr_gs_routes()

        if self.topology.gonetem_is_on:
            self._apply_sr_mpls_routes(init_flag=init_flag, max_workers=max_workers)

    def _build_sr_gs_routes(self) -> None:
        """Build SR label-stack routes for all ground-station pairs.

        Uses a custom path when one was set via set_sr_custom_path, otherwise
        falls back to the shortest weighted path. Results are stored in
        self.sr_lfib keyed by source ground-station ID.
        """
        from satgonetem.models.mpls_entry import SRLabelStackEntry

        if self._label_manager is None:
            return

        graph = self.topology.get_current_graph()
        self._sr_graph = graph

        gs_array = list(self.topology.get_ground_stations())
        sat_array = [
            sat for sat in list(self.topology.get_satellites()) if sat.is_addressable()
        ]
        self.sr_lfib = {}

        use_php = self.mpls_config.use_php if self.mpls_config else False
        route_count = 0

        # Pre-compute all shortest paths in one pass instead of N^2 calls.
        all_paths = dict(nx.all_pairs_dijkstra_path(graph, weight="weight"))

        # Pre-build interface lookup: {node_id: {peer_id: iface}}
        iface_lookup: Dict[int, Dict[int, Any]] = {}
        for node in gs_array + sat_array:
            node_ifaces: Dict[int, Any] = {}
            for iface in node.interfaces:
                try:
                    peer_id = int(iface.name.split(".")[1])
                    node_ifaces[peer_id] = iface
                except (ValueError, IndexError):
                    continue
            iface_lookup[node.id] = node_ifaces

        for gs_src in gs_array + sat_array:
            src_id = gs_src.id
            if src_id not in self.sr_lfib:
                self.sr_lfib[src_id] = []

            src_paths = all_paths.get(src_id)
            if src_paths is None:
                continue

            src_ifaces = iface_lookup.get(src_id, {})

            for gs_dst in gs_array + sat_array:
                if src_id == gs_dst.id:
                    continue

                try:
                    custom_path = self._label_manager.get_custom_path(src_id, gs_dst.id)
                    if custom_path:
                        path = custom_path
                    else:
                        path = src_paths[gs_dst.id]

                    if len(path) < 2:
                        continue

                    first_hop_id = path[1]
                    iface = src_ifaces.get(first_hop_id)

                    if iface is None or iface.peer is None:
                        logging.warning(
                            f"No interface from GS {src_id} to {first_hop_id}"
                        )
                        continue

                    next_hop_ip = iface.peer.ipv4
                    if not next_hop_ip or next_hop_ip == "0.0.0.0":
                        continue

                    label_stack = self._label_manager.get_label_stack_for_path(
                        path[1:], use_php=use_php
                    )

                    entry = SRLabelStackEntry(
                        destination=gs_dst.loopback.ipv4,
                        label_stack=label_stack,
                        next_hop=next_hop_ip,
                        interface=iface,
                        fec_prefix=32,
                    )
                    self.sr_lfib[src_id].append(entry)
                    route_count += 1

                except (nx.NetworkXNoPath, KeyError):
                    logging.debug(f"No path for SR route {src_id} -> {gs_dst.id}")
                except Exception as e:
                    logging.error(
                        f"Failed to build SR route {src_id} -> {gs_dst.id}: {e}"
                    )

        logging.info(
            f"Built {route_count} SR label stack routes for {len(gs_array)} ground stations"
        )

    def _apply_sr_mpls_routes(
        self, init_flag: bool = True, max_workers: int = 32
    ) -> None:
        """Apply SR-MPLS routes to all nodes.

        On init (init_flag=True), MPLS forwarding is enabled on all containers
        before routes are installed. Satellite Node SID rules are applied on
        every call (idempotent). Ground-station label-stack routes are replaced.

        Args:
            init_flag: When True, enable MPLS on all nodes first.
            max_workers: Maximum number of parallel worker threads.
        """
        if self._label_manager is None:
            return

        tic = time.perf_counter()

        if init_flag:
            self._enable_mpls_on_all_nodes(max_workers=max_workers)

        self._apply_sr_node_sids_to_satellites()

        self._apply_sr_routes_to_ground_stations()

        self._apply_sr_routes_to_satellites()

    def _enable_mpls(self, node: "Node") -> None:
        """Load kernel MPLS modules and enable MPLS forwarding on a node.

        Loads mpls_router and mpls_iptunnel modules, sets the platform label
        space size, and enables MPLS input on loopback and every interface.

        Args:
            node: The node whose MPLS forwarding should be enabled.
        """
        commands = [
            "sysctl -w net.mpls.platform_labels=1048575",
            "sysctl -w net.mpls.conf.lo.input=1",
        ]
        for iface in node.interfaces:
            commands.append(f"sysctl -w net.mpls.conf.{iface.get_iname()}.input=1")
        node.execute_command("; ".join(commands))

    def _enable_mpls_on_all_nodes(self, max_workers: int = 32) -> None:
        """Enable MPLS forwarding on all satellites and ground stations.

        Args:
            max_workers: Maximum number of parallel worker threads.
        """
        nodes = list(self.topology.satellites.values()) + list(
            self.topology.ground_stations.values()
        )
        if not nodes:
            return

        total_nodes = len(nodes)
        tic = time.perf_counter()
        workers = min(max_workers, total_nodes)
        ok = 0
        error = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._enable_mpls, node): node
                for node in nodes
                if node.container
            }
            for fut in as_completed(futures):
                node = futures[fut]
                try:
                    fut.result()
                    ok += 1
                except Exception as e:
                    error += 1
                    logging.error(f"Failed to enable MPLS on {node.name}: {e}")
        toc = time.perf_counter()
        logging.info(
            f"MPLS enabled on {ok}/{total_nodes} nodes in {(toc - tic) * 1000:.2f}ms"
        )

    def _apply_sr_node_sids_to_satellites(self) -> None:
        """Install SR-MPLS forwarding entries on all satellites.

        On the first call (previous table empty) all entries are installed.
        On subsequent calls only entries whose attributes changed are sent.
        """
        if self._label_manager is None or self._sr_graph is None:
            return

        all_sats = list(self.topology.satellites.values())
        all_gs = list(self.topology.ground_stations.values())
        workers = min(32, (os.cpu_count() or 4) * 4, len(all_sats))

        def install_sr_forwarding(sat: Satellite) -> None:
            if self._label_manager is None:
                return
            my_sid = self._label_manager.get_node_sid(sat.id)
            if my_sid is None:
                return

            route_commands = []

            own_entry = SRNodeSIDEntry(node_sid=my_sid, node_name=sat.name)
            route_commands.append(own_entry.to_iproute2_command())

            for iface in sat.interfaces:
                if iface.peer is None:
                    continue
                try:
                    neighbor_id = int(iface.name.split(".")[1])
                except (ValueError, IndexError):
                    continue

                neighbor_sid = self._label_manager.get_node_sid(neighbor_id)
                if neighbor_sid is None:
                    continue

                next_hop_ip = iface.peer.ipv4
                if not next_hop_ip or next_hop_ip == "0.0.0.0":
                    continue

                match self.topology.satellites.get(neighbor_id):
                    case Satellite() as neighbor_node:
                        neighbor_name = neighbor_node.name
                    case None:
                        match self.topology.ground_stations.get(neighbor_id):
                            case GroundStation() as neighbor_node:
                                neighbor_name = neighbor_node.name
                            case None:
                                neighbor_name = f"Node{neighbor_id}"
                            case _ as unexpected:
                                raise TypeError(
                                    f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                                )
                    case _ as unexpected:
                        raise TypeError(
                            f"Expected Satellite in satellites, got {type(unexpected)}"
                        )

                fwd_entry = SRForwardEntry(
                    target_sid=neighbor_sid,
                    next_hop=next_hop_ip,
                    interface=iface,
                    target_name=neighbor_name,
                )
                route_commands.append(fwd_entry.to_iproute2_command())

            for gs in all_gs:
                iface = self.topology._get_interface_to_peer(sat, gs.id)
                if iface is None or iface.peer is None:
                    continue
                gs_loopback = gs.loopback.ipv4 if gs.loopback else None
                if not gs_loopback:
                    continue
                gs_ip = iface.peer.ipv4
                iface_name = iface.get_iname()
                route_commands.append(
                    f"ip route replace {gs_loopback}/32 via {gs_ip} dev {iface_name}"
                )

            prev = self._prev_sr_node_sid_routes.get(sat.id, [])
            prev_set = set(prev)
            changed = [line for line in route_commands if line not in prev_set]

            if changed:
                batch_cmd = "; ".join(changed)
                try:
                    sat.execute_command(["sh", "-lc", batch_cmd])
                    self._prev_sr_node_sid_routes[sat.id] = route_commands
                except Exception as e:
                    logging.error(f"Failed to install SR forwarding on {sat.name}: {e}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(install_sr_forwarding, sat)
                for sat in all_sats
                if sat.container
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"SR forwarding installation error: {e}")

    def _apply_sr_routes_to_ground_stations(self) -> None:
        """Install SR-MPLS routes on all ground stations.

        On the first call (previous table empty) all routes are installed.
        On subsequent calls only routes whose attributes changed are sent.
        """
        from satgonetem.models.mpls_entry import SRNodeSIDEntry

        if self._label_manager is None:
            return

        workers = min(
            32,
            (os.cpu_count() or 4) * 4,
            len(self.topology.ground_stations),
        )

        def install_gs_routes(gs: "GroundStation") -> None:
            if self._label_manager is None:
                raise RuntimeError("SR-MPLS manager not initialized")

            route_commands = []

            my_sid = self._label_manager.get_node_sid(gs.id)
            if my_sid is not None:
                own_entry = SRNodeSIDEntry(node_sid=my_sid, node_name=gs.name)
                route_commands.append(own_entry.to_iproute2_command())

            for entry in self.sr_lfib.get(gs.id, []):
                route_commands.append(entry.to_iproute2_command())

            prev = self._prev_sr_gs_routes.get(gs.id, [])
            prev_set = set(prev)
            changed = [line for line in route_commands if line not in prev_set]

            if changed:
                batch_cmd = "; ".join(changed)
                try:
                    gs.execute_command(["sh", "-lc", batch_cmd])
                    self._prev_sr_gs_routes[gs.id] = route_commands
                except Exception as e:
                    logging.error(f"Failed to install SR routes on {gs.name}: {e}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(install_gs_routes, gs)
                for gs in self.topology.ground_stations.values()
                if gs.container
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"GS SR route installation error: {e}")

    def _apply_sr_routes_to_satellites(self) -> None:
        """Install SR-MPLS routes on all satellites.

        On the first call (previous table empty) all routes are installed.
        On subsequent calls only routes whose attributes changed are sent.
        """
        from satgonetem.models.mpls_entry import SRNodeSIDEntry

        if self._label_manager is None:
            return

        workers = min(
            32,
            (os.cpu_count() or 4) * 4,
            len(self.topology.satellites),
        )

        def install_satellite_routes(sat: "Satellite") -> None:
            if self._label_manager is None:
                raise RuntimeError("SR-MPLS manager not initialized")

            route_commands = []

            my_sid = self._label_manager.get_node_sid(sat.id)
            if my_sid is not None:
                own_entry = SRNodeSIDEntry(node_sid=my_sid, node_name=sat.name)
                route_commands.append(own_entry.to_iproute2_command())

            for entry in self.sr_lfib.get(sat.id, []):
                route_commands.append(entry.to_iproute2_command())

            prev = self._prev_sr_sat_routes.get(sat.id, [])
            prev_set = set(prev)
            changed = [line for line in route_commands if line not in prev_set]

            if changed:
                batch_cmd = "; ".join(changed)
                try:
                    sat.execute_command(["sh", "-lc", batch_cmd])
                    self._prev_sr_sat_routes[sat.id] = route_commands
                except Exception as e:
                    logging.error(f"Failed to install SR routes on {sat.name}: {e}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(install_satellite_routes, sat)
                for sat in [
                    satellite
                    for satellite in self.topology.satellites.values()
                    if satellite.is_addressable()
                ]
                if sat.container
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"Satellite SR route installation error: {e}")

    def _update_sr_route_for_source(self, source_id: int) -> None:
        """Update SR routes for a single source ground station.

        Called after set_sr_custom_path or clear_sr_custom_path so only the
        affected source is reconfigured; satellites remain untouched.

        Args:
            source_id: ID of the ground station to update.

        Raises:
            RuntimeError: If SR-MPLS has not been initialized.
        """
        from satgonetem.models.ground_station import GroundStation
        from satgonetem.models.mpls_entry import SRLabelStackEntry

        if self._label_manager is None:
            raise RuntimeError("SR-MPLS manager not initialized")

        match self.topology.ground_stations.get(source_id):
            case GroundStation() as gs_src:
                pass
            case None:
                return
            case _ as unexpected:
                raise TypeError(
                    f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                )

        if gs_src.container is None:
            return

        graph = self.topology.get_current_graph()
        gs_array = list(self.topology.get_ground_stations())
        use_php = self.mpls_config.use_php if self.mpls_config else False
        new_entries = []

        for gs_dst in gs_array:
            if gs_src.id == gs_dst.id:
                continue
            try:
                custom_path = self._label_manager.get_custom_path(gs_src.id, gs_dst.id)
                path = (
                    custom_path
                    if custom_path
                    else nx.shortest_path(
                        graph, source=gs_src.id, target=gs_dst.id, weight="weight"
                    )
                )

                if len(path) < 2:
                    continue

                first_hop_id = path[1]
                iface = self.topology._get_interface_to_peer(gs_src, first_hop_id)
                if iface is None or iface.peer is None:
                    continue

                next_hop_ip = iface.peer.ipv4
                if not next_hop_ip or next_hop_ip == "0.0.0.0":
                    continue

                label_stack = self._label_manager.get_label_stack_for_path(
                    path[1:], use_php=use_php
                )

                entry = SRLabelStackEntry(
                    destination=gs_dst.loopback.ipv4,
                    label_stack=label_stack,
                    next_hop=next_hop_ip,
                    interface=iface,
                    fec_prefix=32,
                )
                new_entries.append(entry)

            except Exception as e:
                logging.error(
                    f"Failed to build SR route {gs_src.id} -> {gs_dst.id}: {e}"
                )

        self.sr_lfib[source_id] = new_entries

        self._enable_mpls(gs_src)

        route_commands = []
        my_sid = self._label_manager.get_node_sid(gs_src.id)
        if my_sid is not None:
            own_entry = SRNodeSIDEntry(node_sid=my_sid, node_name=gs_src.name)
            route_commands.append(own_entry.to_iproute2_command())

        for entry in new_entries:
            route_commands.append(entry.to_iproute2_command())

        if route_commands:
            batch_cmd = "; ".join(route_commands)
            try:
                gs_src.execute_command(["sh", "-lc", batch_cmd])
                self._prev_sr_gs_routes[source_id] = route_commands
                logging.info(f"Updated {len(new_entries)} SR routes on {gs_src.name}")
            except Exception as e:
                logging.error(f"Failed to update SR routes on {gs_src.name}: {e}")

    def _flush_node(self, node: "Node") -> None:
        """Flush the MPLS forwarding table on a single node container.

        Args:
            node: The node whose MPLS table should be flushed.
        """
        if node.container is None:
            return
        try:
            node.execute_command(
                ["sh", "-lc", "ip -f mpls route flush 2>/dev/null; true"]
            )
        except Exception as e:
            logging.error(f"Failed to flush MPLS routes on {node.name}: {e}")

    def _flush_all_nodes(self, max_workers: int) -> None:
        """Flush MPLS forwarding tables on all satellites and ground stations.

        Args:
            max_workers: Maximum number of parallel worker threads.
        """
        nodes = list(self.topology.satellites.values()) + list(
            self.topology.ground_stations.values()
        )
        if not nodes:
            return

        workers = min(max_workers, len(nodes))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._flush_node, n): n for n in nodes if n.container
            }
            for fut in as_completed(futures):
                n = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"MPLS flush error on {n.name}: {e}")
