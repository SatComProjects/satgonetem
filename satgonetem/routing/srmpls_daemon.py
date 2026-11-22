"""SR-MPLS routing daemon.

Owns all Segment Routing MPLS state and logic. The topology manager holds no
SR-MPLS state; all label allocation, route building, and route application are
encapsulated here.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from satgonetem.routing.base_daemon import RoutingContext, RoutingDaemon
from satgonetem.routing.srmpls_installer import SRMPLSRouteInstaller
from satgonetem.routing.srmpls_label_manager import (
    MPLS_LABEL_IMPLICIT_NULL,
    MPLS_LABEL_MAX,
    MPLS_LABEL_MIN,
    NodeSID,
    SegmentRoutingManager,
)
from satgonetem.routing.srmpls_planner import SRMPLSRoutePlanner
from satgonetem.routing.srmpls_route_ops import (
    build_batch,
    cached_route_keys,
    delete_command,
    route_changes,
    route_key,
    tolerant_delete_command,
)
from satgonetem.routing.srmpls_teardown import SRMPLSTeardown

__all__ = [
    "MPLS_LABEL_IMPLICIT_NULL",
    "MPLS_LABEL_MAX",
    "MPLS_LABEL_MIN",
    "NodeSID",
    "SRMPLSDaemon",
    "SegmentRoutingManager",
]

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


def _route_key(route_command: str) -> tuple[str, str]:
    """Compatibility delegate to :func:`srmpls_route_ops.route_key`."""
    return route_key(route_command)


def _route_delete_command(route_command: str) -> str:
    """Compatibility delegate to :func:`srmpls_route_ops.delete_command`."""
    return delete_command(route_command)


def _tolerant_route_delete_command(route_command: str) -> str:
    """Compatibility delegate to :func:`srmpls_route_ops.tolerant_delete_command`."""
    return tolerant_delete_command(route_command)


def _route_changes(
    previous: list[str],
    desired: list[str],
    foreign_keys: set[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Compatibility delegate to :func:`srmpls_route_ops.route_changes`."""
    return route_changes(previous, desired, foreign_keys)


def _cached_route_keys(commands: list[str]) -> set[tuple[str, str]]:
    """Compatibility delegate to :func:`srmpls_route_ops.cached_route_keys`."""
    return cached_route_keys(commands)


def _build_batch(deletes: list[str], installs: list[str]) -> str:
    """Compatibility delegate to :func:`srmpls_route_ops.build_batch`."""
    return build_batch(deletes, installs)


class SRMPLSDaemon(RoutingDaemon):
    """Segment Routing MPLS routing daemon.

    Owns the full SR-MPLS lifecycle: label allocation, route building, and
    forwarding-table installation on every node. The topology manager carries
    no SR-MPLS state; every piece of it lives here.

    SR-MPLS model used by this daemon:
    - Each node gets one stable Node SID (label = base + node_id).
    - Ground stations push a label stack encoding the complete path.
    - Satellites install their own SID pop rule and per-neighbour forward
      rules, which are O(degree).
    - Every ground station and every addressable satellite is also an ingress
      source: it pushes a label stack to every other destination. That ingress
      table is O(N) and is rebuilt on every update; non-addressable satellites
      are never ingress sources or destinations.
    """

    def __init__(self, topology: RoutingContext) -> None:
        """
        Args:
            topology: The TopologyManager instance that owns this daemon.
        """
        super().__init__(topology)
        self._label_manager: SegmentRoutingManager | None = None
        self.sr_lfib: dict[int, list] = {}
        self._sr_graph: Any | None = None
        self.mpls_config: Any | None = None
        self._prev_sr_gs_routes: dict[int, list[str]] = {}
        self._prev_sr_sat_routes: dict[int, list[str]] = {}
        self._prev_sr_node_sid_routes: dict[int, list[str]] = {}

    @property
    def is_initialized(self) -> bool:
        """Return True after init() has been called successfully."""
        return self._label_manager is not None

    def get_node_sid(self, node_id: int) -> int | None:
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

            errors = self._rebuild_sr_mpls_routing(
                init_flag=True, max_workers=max_workers
            )

            return errors == 0
        except Exception as e:
            logging.error(f"SR-MPLS initialization failed: {e}")
            return False

    def update(self, _new_links: list[Link], max_workers: int = 128) -> None:
        """Rebuild SR-MPLS routes after topology changes.

        Recomputes ingress label-stack routes for every source (ground
        stations and addressable satellites) and re-applies satellite
        forwarding entries. Node SID allocations are stable, but satellite
        SID/forward rules and ingress routes are both re-sent when their
        attributes change.

        Args:
            _new_links: Unused. The topology graph is re-fetched from the
                topology manager on each rebuild.
            max_workers: Maximum number of parallel worker threads.
        """
        self._rebuild_sr_mpls_routing(init_flag=False, max_workers=max_workers)

    def remove(self, node: Node | None = None, max_workers: int = 128) -> None:
        """Remove SR-MPLS forwarding entries for one node or the topology.

        With *node*, flushes that node's kernel entries and drops only that
        node's cached routes and LFIB slot. The daemon stays initialized for
        the remaining nodes. Without *node*, flushes every node and resets all
        in-memory SR state.

        Args:
            node: If provided, remove only this node. Otherwise remove all
                satellites and ground stations.
            max_workers: Maximum number of parallel worker threads.
        """
        if node is not None:
            self._flush_node(node)
            self.sr_lfib.pop(node.id, None)
            self._prev_sr_gs_routes.pop(node.id, None)
            self._prev_sr_sat_routes.pop(node.id, None)
            self._prev_sr_node_sid_routes.pop(node.id, None)
            return

        self._flush_all_nodes(max_workers)

        self.sr_lfib = {}
        self._sr_graph = None
        self._label_manager = None
        self._prev_sr_gs_routes.clear()
        self._prev_sr_sat_routes.clear()
        self._prev_sr_node_sid_routes.clear()

    def set_sr_custom_path(
        self, source_id: int, dest_id: int, path: list[int]
    ) -> dict[str, Any]:
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

    def clear_sr_custom_path(self, source_id: int, dest_id: int) -> dict[str, Any]:
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

    def get_sr_statistics(self) -> dict[str, Any]:
        """Return SR-MPLS statistics.

        ``nodes_with_routes`` counts every LFIB source (ground stations and
        addressable satellites). ``ground_stations_with_routes`` counts only
        the ground-station subset.

        Returns:
            Dict with enabled, node_sids_allocated, custom_paths, sr_routes,
            nodes_with_routes, and ground_stations_with_routes.
        """
        if self._label_manager is None:
            return {"enabled": False}

        stats = self._label_manager.get_statistics()
        stats["enabled"] = True
        stats["sr_routes"] = sum(len(v) for v in self.sr_lfib.values())
        stats["nodes_with_routes"] = len(self.sr_lfib)
        stats["ground_stations_with_routes"] = sum(
            1 for node_id in self.sr_lfib if node_id in self.topology.ground_stations
        )
        return stats

    def list_sr_custom_paths(self) -> list[dict[str, Any]]:
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
    ) -> int:
        """Rebuild SR-MPLS routing for the current time step.

        Satellite Node SID allocations are stable, but satellite SID/forward
        rules and ingress label-stack routes are both recomputed and
        re-applied on every call.

        Args:
            init_flag: When True, also enables MPLS on all nodes before
                applying routes.
            max_workers: Maximum number of parallel worker threads.

        Returns:
            Number of nodes/workers that failed while applying routes. Zero
            when nothing was applied (e.g. simulator not running).
        """
        if self._label_manager is None:
            logging.warning("SR-MPLS routing called but SR manager not initialized")
            return 0

        if init_flag:
            self._prev_sr_gs_routes.clear()
            self._prev_sr_sat_routes.clear()
            self._prev_sr_node_sid_routes.clear()

        self._label_manager.clear_cache()
        self._build_sr_gs_routes()

        if self.topology.gonetem_is_on:
            return self._apply_sr_mpls_routes(
                init_flag=init_flag, max_workers=max_workers
            )
        return 0

    def _build_sr_gs_routes(self) -> None:
        """Build SR label-stack ingress routes for every source.

        Sources and destinations are all ground stations plus addressable
        satellites. Uses a custom path when one was set via
        set_sr_custom_path, otherwise falls back to the shortest weighted
        path. Results are stored in self.sr_lfib keyed by source node ID.
        """
        if self._label_manager is None:
            return

        graph = self.topology.get_current_graph()
        self._sr_graph = graph

        use_php = self.mpls_config.use_php if self.mpls_config else False
        result = SRMPLSRoutePlanner(self.topology).build_all(
            graph, self._label_manager, use_php
        )
        self.sr_lfib = result

    def _apply_sr_mpls_routes(
        self, init_flag: bool = True, max_workers: int = 32
    ) -> int:
        """Apply SR-MPLS routes to all nodes.

        MPLS forwarding is enabled on all containers before routes are
        installed, so interfaces added after init also get MPLS input enabled
        (the operation is idempotent). Satellite Node SID rules are applied on
        every call. Ground-station label-stack routes are replaced.

        Args:
            init_flag: Retained for call-site compatibility; MPLS is enabled
                on every call regardless of its value.
            max_workers: Maximum number of parallel worker threads.

        Returns:
            Total number of nodes/workers that failed across the enable and
            apply steps.
        """
        if self._label_manager is None:
            return 0

        tic = time.perf_counter()
        errors = 0

        errors += self._enable_mpls_on_all_nodes(max_workers=max_workers)

        errors += self._apply_sr_node_sids_to_satellites()

        errors += self._apply_sr_routes_to_ground_stations()

        errors += self._apply_sr_routes_to_satellites()

        toc = time.perf_counter()
        logging.info(
            f"Applied SR-MPLS routes in {(toc - tic) * 1000:.2f}ms with {errors} errors"
        )
        return errors

    def _enable_mpls(self, node: Node) -> None:
        """Enable MPLS forwarding on a node."""
        SRMPLSRouteInstaller(self.topology).enable_mpls(node)

    def _enable_mpls_on_all_nodes(self, max_workers: int = 32) -> int:
        """Enable MPLS forwarding on all satellites and ground stations.

        Args:
            max_workers: Maximum number of parallel worker threads.

        Returns:
            Number of nodes on which MPLS could not be enabled.
        """
        return SRMPLSRouteInstaller(self.topology).enable_mpls_on_all_nodes(
            max_workers, self._enable_mpls
        )

    def _apply_sr_node_sids_to_satellites(self) -> int:
        """Install SR-MPLS forwarding entries on all satellites.

        Returns:
            Number of satellites whose forwarding entries could not be installed.
        """
        if self._label_manager is None or self._sr_graph is None:
            return 0
        return SRMPLSRouteInstaller(self.topology).apply_sr_node_sids_to_satellites(
            self._label_manager,
            self.sr_lfib,
            self._prev_sr_node_sid_routes,
            self._prev_sr_sat_routes,
        )

    def _apply_sr_routes_to_ground_stations(self) -> int:
        """Install SR-MPLS routes on all ground stations.

        Returns:
            Number of ground stations whose routes could not be installed.
        """
        if self._label_manager is None:
            return 0
        return SRMPLSRouteInstaller(self.topology).apply_sr_routes_to_ground_stations(
            self._label_manager,
            self.sr_lfib,
            self._prev_sr_gs_routes,
        )

    def _apply_sr_routes_to_satellites(self) -> int:
        """Install SR-MPLS label-stack routes on all satellites.

        Returns:
            Number of satellites whose routes could not be installed.
        """
        if self._label_manager is None:
            return 0
        return SRMPLSRouteInstaller(self.topology).apply_sr_routes_to_satellites(
            self._label_manager,
            self.sr_lfib,
            self._prev_sr_sat_routes,
            self._prev_sr_node_sid_routes,
        )

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
        use_php = self.mpls_config.use_php if self.mpls_config else False
        new_entries = SRMPLSRoutePlanner(self.topology).build_for_source(
            gs_src, graph, self._label_manager, use_php
        )

        self.sr_lfib[source_id] = new_entries

        self._enable_mpls(gs_src)

        SRMPLSRouteInstaller(self.topology).install_source_routes(
            gs_src,
            self._label_manager,
            new_entries,
            self._prev_sr_gs_routes,
        )

    def _flush_node(self, node: Node) -> None:
        """Flush SR-MPLS and IPv4 routes on a single node container.

        Delegates to :class:`SRMPLSTeardown` with the current cache objects so
        the daemon keeps observing removals and any node-level patch still
        applies.

        Args:
            node: The node whose routes should be flushed.
        """
        SRMPLSTeardown(
            (
                self._prev_sr_gs_routes,
                self._prev_sr_sat_routes,
                self._prev_sr_node_sid_routes,
            )
        ).flush_node(node)

    def _flush_all_nodes(self, max_workers: int) -> None:
        """Flush MPLS forwarding tables on all satellites and ground stations.

        Args:
            max_workers: Maximum number of parallel worker threads.
        """
        nodes = list(self.topology.satellites.values()) + list(
            self.topology.ground_stations.values()
        )
        SRMPLSTeardown(
            (
                self._prev_sr_gs_routes,
                self._prev_sr_sat_routes,
                self._prev_sr_node_sid_routes,
            )
        ).flush_all_nodes(nodes, max_workers, self._flush_node)
