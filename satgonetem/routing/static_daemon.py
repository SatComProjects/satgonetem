"""
Static IP routing daemon.

Computes shortest paths via Dijkstra (or loads from a file) and installs them
on every node. All routing logic is self-contained; the topology manager is
only used to access node/graph data.
"""

from __future__ import annotations

import gc
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

# Ensure google.protobuf.empty_pb2 is registered before netem_pb2 is loaded.
from google.protobuf import empty_pb2  # noqa: F401

from satgonetem.routing.base_daemon import RoutingContext, RoutingDaemon
from satgonetem.routing.commands import encode_ip_batch
from satgonetem.routing.peer_interface_index import PeerInterfaceIndex
from satgonetem.routing.route_batch_installer import (
    MAX_ROUTE_NODES_PER_REQUEST,
    MAX_ROUTE_REQUEST_BYTES,
    SLOW_ROUTE_DURATION_MICROS,
    RouteBatchInstaller,
    _BatchOutcome,
)
from satgonetem.routing.static_route_ops import (
    RouteOperation,
    emit_batch_commands,
    emit_route_operations,
    entry_key,
    format_route_operation,
    route_cache_signature,
    route_operation,
)
from satgonetem.routing.static_route_planner import StaticRoutePlanner

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


@contextmanager
def _gc_disabled():
    """Temporarily disable cyclic GC, restoring the previous state."""
    was_enabled = gc.isenabled()
    if was_enabled:
        gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


class StaticRoutingDaemon(RoutingDaemon):
    """Static IP routing daemon.

    On init, computes shortest paths via Dijkstra (or loads from a pre-computed
    file) and installs IP routes on every satellite and ground station. On
    update, recomputes and replaces routes after topology changes.
    """

    def __init__(self, topology: RoutingContext) -> None:
        """Initialize the daemon and its route-reuse caches.

        Args:
            topology: The topology manager that owns this daemon.
        """
        super().__init__(topology)
        self._peer_interface_index = PeerInterfaceIndex(topology)
        self._route_planner = StaticRoutePlanner(topology, self._peer_interface_index)
        self._route_batch_installer = RouteBatchInstaller(topology)
        self._last_interface_version = getattr(topology, "interface_version", None)
        # Nodes whose last batch failed: their previous table must not be
        # treated as installed, so the next update re-emits every route.
        self._failed_nodes: set[str] = set()

    @property
    def _route_install_supported(self) -> bool:
        """Whether the server is still expected to implement RouteInstall."""
        return self._route_batch_installer.route_install_supported

    @property
    def _source_signatures(self) -> dict[int, list]:
        """Per-source next-hop snapshots from the last population."""
        return self._route_planner.source_signatures

    @property
    def _entry_cache(self) -> dict[int, dict]:
        """Per-source cached route entries from the last population."""
        return self._route_planner.entry_cache

    @property
    def _iface_cache(self):
        """The cached iface maps, owned by the peer interface index."""
        return self._peer_interface_index.iface_cache

    @property
    def _iface_cache_version(self) -> int | None:
        """The interface version the cached maps were built from."""
        return self._peer_interface_index.iface_cache_version

    def init(self, max_workers: int = 128) -> bool:
        """Initialize static IP routing.

        Populates routing tables using shortest paths, clears stale previous
        tables, then applies the computed routes to all nodes in parallel.

        Args:
            max_workers: Maximum number of worker threads for parallel route
                installation.

        Returns:
            True if routing was installed successfully, False otherwise.
        """
        self._route_planner.clear_reuse_cache()
        self._last_interface_version = getattr(self.topology, "interface_version", None)
        self._failed_nodes.clear()
        try:
            self._populate_routing_tables()
            for node in list(self.topology.get_satellites()) + list(
                self.topology.get_ground_stations()
            ):
                node.ipv4_previous_routing_table.clear()

            outcome = self._apply_all_routes(max_workers=max_workers)
            if outcome.error_nodes:
                logging.error(
                    "Static routing failed on %d node(s): %s",
                    len(outcome.error_nodes),
                    ", ".join(sorted(outcome.error_nodes)),
                )
                return False
            return True
        except Exception as e:
            logging.error(f"Error initializing static IP routing: {e}")
            return False

    def update(self, new_links: list[Link], max_workers: int = 128) -> None:
        """Update static IP routing after topology changes.

        Recomputes shortest-path routing tables and pushes incremental route
        changes to all nodes. The new_links argument is accepted for interface
        consistency but is not used by static routing. When the topology is not
        active the tables are left untouched: rotating and rebuilding them
        would make the un-applied table look like the installed state, and the
        next active update would incorrectly emit no operations.

        Args:
            new_links: Newly added links (unused for static routing).
            max_workers: Maximum number of worker threads for parallel route
                updates.

        Raises:
            ValueError: If a reachable destination's next hop has no interface
                on the source node, or that interface has no peer. The
                inconsistency is reported instead of silently installing an
                on-link route the topology cannot use.
        """
        if not self.topology.status:
            logging.warning("Static routing update skipped: topology not yet active")
            return

        interface_version = getattr(self.topology, "interface_version", None)
        if interface_version != self._last_interface_version:
            # Recreated interfaces invalidate cached route entries even when
            # the destination/gateway/next-hop signature is unchanged.
            self._route_planner.clear_reuse_cache()
            self._last_interface_version = interface_version

        self._populate_routing_tables()
        outcome = self._apply_all_routes(max_workers=max_workers)
        if outcome.error_nodes:
            logging.warning(
                "Static routing update failed on %d node(s): %s",
                len(outcome.error_nodes),
                ", ".join(sorted(outcome.error_nodes)),
            )

    def remove(self, node: Node | None = None, max_workers: int = 128) -> None:
        """Remove all installed static IP routes.

        Deletes routes from the container and clears the in-memory routing
        tables. If node is provided, only that node is affected; otherwise
        all satellites and ground stations are removed through one batch.

        Args:
            node: Target node. If None, all nodes in the topology are cleared.
            max_workers: Maximum number of worker threads for the legacy
                fallback path.
        """
        if node is not None:
            self._route_planner.forget_source(node.id)
            self._remove_node_routes(node, max_workers=max_workers)
            return

        self._route_planner.clear_reuse_cache()
        nodes = list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        )
        batches = {n: ops for n in nodes if (ops := self._delete_operations(n))}
        outcome = (
            self._install_route_batches(batches, max_workers=max_workers)
            if batches
            else _BatchOutcome()
        )
        for n in nodes:
            if n.name in outcome.error_nodes:
                logging.error(f"Failed to remove routes on {n.name}")
                continue
            n.ipv4_routing_table.clear()
            n.ipv4_previous_routing_table.clear()

    def _populate_routing_tables(self) -> None:
        """Populate IPv4 routing tables for all ground stations and satellites."""
        self._equalize_routing_tables()
        self._route_planner.set_failed_sources(self._failed_nodes)
        self._route_planner.populate_tables(_gc_disabled, self._load_routes_from_file)

    def _apply_all_routes(self, max_workers: int = 128) -> _BatchOutcome:
        """Apply all ground-station and satellite route changes in one batch.

        Combining both node types lets the server use a single worker pool for
        a simulation step instead of one pool per node type.

        Args:
            max_workers: Maximum number of worker threads for the legacy
                fallback path.
        """
        nodes = list(self.topology.ground_stations.values()) + list(
            self.topology.satellites.values()
        )
        return self._apply_route_changes(nodes, max_workers=max_workers, label="static")

    def _apply_satellite_routes(self, max_workers: int = 128) -> _BatchOutcome:
        """Apply satellite IPv4 route changes to containers using a diff.

        Args:
            max_workers: Maximum number of worker threads for the legacy
                fallback path.
        """
        return self._apply_route_changes(
            list(self.topology.satellites.values()),
            max_workers=max_workers,
            label="satellite",
        )

    def _apply_ground_station_routes(self, max_workers: int = 128) -> _BatchOutcome:
        """Apply ground-station IPv4 route changes to containers using a diff.

        Args:
            max_workers: Maximum number of worker threads for the legacy
                fallback path.
        """
        return self._apply_route_changes(
            list(self.topology.ground_stations.values()),
            max_workers=max_workers,
            label="ground station",
        )

    def _apply_route_changes(
        self, nodes, max_workers: int = 128, label: str = "node"
    ) -> _BatchOutcome:
        """Diff and apply route changes for a set of nodes.

        Args:
            nodes: Nodes whose routing tables should be compared.
            max_workers: Maximum number of worker threads for the legacy
                fallback path.
            label: Name used in the no-change log message.

        Returns:
            The per-node outcome of the batch.
        """
        batches = {}
        for node in nodes:
            ops = self._emit_route_operations(
                node.ipv4_previous_routing_table,
                node.ipv4_routing_table,
                live_devs={iface.get_iname() for iface in node.interfaces},
            )
            if ops:
                batches[node] = ops

        if not batches:
            logging.info(f"No {label} route changes needed.")
            return _BatchOutcome()

        try:
            outcome = self._install_route_batches(batches, max_workers=max_workers)
        except Exception:
            # A batch-level failure (for example the RouteInstall RPC raising
            # before any per-node result) leaves every node in the batch
            # uninstalled; mark them so the next update re-emits their routes.
            self._failed_nodes.update(node.name for node in batches)
            raise
        for node in batches:
            if node.name in outcome.error_nodes:
                self._failed_nodes.add(node.name)
            elif node.name in outcome.ok_nodes:
                self._failed_nodes.discard(node.name)
        return outcome

    def _resolve_route_batch_node(self, key):
        """Resolve a node name key to a node object when possible."""
        return self._route_batch_installer.resolve_route_batch_node(key)

    def _partition_route_batches(self, jobs):
        """Split node jobs into gRPC-client groups and legacy jobs."""
        return self._route_batch_installer.partition_route_batches(jobs)

    def _next_route_chunk(self, jobs, start: int):
        """Build the next RouteInstall chunk from *jobs* starting at *start*."""
        return self._route_batch_installer.next_route_chunk(
            jobs, start, MAX_ROUTE_NODES_PER_REQUEST, MAX_ROUTE_REQUEST_BYTES
        )

    def _build_route_batch(self, ops: list[RouteOperation]):
        """Convert typed route operations into a protobuf RouteBatch."""
        return self._route_batch_installer.build_route_batch(ops)

    def _send_route_install(self, client, project_id: str, chunk) -> _BatchOutcome:
        """Send one RouteInstall request and interpret its per-node results."""
        return self._route_batch_installer.send_route_install(
            client, project_id, chunk, SLOW_ROUTE_DURATION_MICROS
        )

    def _install_route_batches(self, batches, max_workers: int = 128) -> _BatchOutcome:
        """Install route batches through RouteInstall with legacy fallback."""
        return self._route_batch_installer.install_route_batches(
            batches,
            max_workers=max_workers,
            exec_batch=self._exec_batch,
            format_operation=self._format_route_operation,
            max_nodes=MAX_ROUTE_NODES_PER_REQUEST,
            max_bytes=MAX_ROUTE_REQUEST_BYTES,
            slow_duration_micros=SLOW_ROUTE_DURATION_MICROS,
        )

    def _apply_legacy_batches(self, jobs, max_workers: int = 128) -> _BatchOutcome:
        """Apply route operations with the legacy per-node NodeRun path."""
        return self._route_batch_installer.apply_legacy_batches(
            jobs,
            max_workers=max_workers,
            exec_batch=self._exec_batch,
            format_operation=self._format_route_operation,
        )

    def _delete_operations(self, node: Node) -> list[RouteOperation]:
        """Build delete operations for a node's current routing table.

        Entries whose device is no longer present are skipped: removing a veth
        already purges its routes, and asking the server to delete a missing
        device would turn the node into an error.
        """
        live_devs = {iface.get_iname() for iface in node.interfaces}
        return [
            RouteOperation(
                action="del",
                destination=entry.destination + entry.get_prefix(),
                gateway=entry.gateway or "",
                device=entry.interface.get_iname(),
            )
            for entry in node.ipv4_routing_table
            if entry.interface.get_iname() in live_devs
        ]

    def _remove_node_routes(self, node: Node, max_workers: int = 128) -> None:
        """Delete all IPv4 routes from a single node.

        Builds a ROUTE_DELETE batch for the node's current routing table,
        installs it, then clears both the current and previous in-memory
        tables.

        Args:
            node: The node whose routes should be removed.
            max_workers: Maximum number of worker threads for the legacy
                fallback path.
        """
        ops = self._delete_operations(node)
        if ops:
            outcome = self._install_route_batches({node: ops}, max_workers=max_workers)
            if node.name in outcome.error_nodes:
                logging.error(f"Failed to remove routes on {node.name}")
                return
        node.ipv4_routing_table.clear()
        node.ipv4_previous_routing_table.clear()

    def _equalize_routing_tables(self) -> None:
        """Rotate current table to previous and clear current for all nodes.

        The old list is kept by reference so the diff can compare against it;
        allocating a fresh list for the new table avoids copying every entry.
        """
        for node in list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        ):
            if node.name in self._failed_nodes:
                # The last install failed; retry every desired route.
                node.ipv4_previous_routing_table = []
            else:
                node.ipv4_previous_routing_table = node.ipv4_routing_table
            node.ipv4_routing_table = []

    def _populate_from_file(self, gs_array, sat_array, gs_ifaces, sat_ifaces) -> None:
        """Populate routing tables from a precomputed JSON route file."""
        self._route_planner.populate_from_file(
            gs_array,
            sat_array,
            gs_ifaces,
            sat_ifaces,
            self._load_routes_from_file,
        )

    def _populate_dijkstra(
        self, graph, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables using NetworkX Dijkstra shortest paths."""
        self._route_planner.populate_dijkstra(
            graph, gs_array, sat_array, gs_ifaces, sat_ifaces
        )

    def _route_cache_signature(
        self,
        src_id: int,
        src_ifaces,
        dst_array,
        next_hops: dict[int, dict[int, int]],
        loopback_ips: dict[int, str],
    ) -> list[tuple[int, str, str, int | None]]:
        """Build the route-cache signature for one source."""
        return route_cache_signature(
            src_id, src_ifaces, dst_array, next_hops, loopback_ips
        )

    def _install_source_routes(
        self,
        node,
        src_id: int,
        src_ifaces,
        src_loopback: str,
        src_name: str,
        dst_array,
        dst_ids: list[int],
        signature: list,
        loopback_ips: dict[int, str],
        prefix: int,
    ) -> None:
        """Install or reuse the routes originating from one node."""
        self._route_planner.install_source_routes(
            node=node,
            src_id=src_id,
            src_ifaces=src_ifaces,
            src_loopback=src_loopback,
            src_name=src_name,
            dst_array=dst_array,
            dst_ids=dst_ids,
            signature=signature,
            loopback_ips=loopback_ips,
            prefix=prefix,
        )

    def _populate_dijkstra_igraph(
        self, graph, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables using igraph Dijkstra shortest paths."""
        self._route_planner.populate_dijkstra_igraph(
            graph, gs_array, sat_array, gs_ifaces, sat_ifaces
        )

    def _load_routes_from_file(self) -> dict:
        """Load precomputed routes from the JSON route file."""
        return self._route_planner.load_routes_from_file()

    def _prebuild_iface_maps(self):
        """Build fast peer_id->Interface lookups for every node."""
        return self._peer_interface_index.prebuild()

    def _peer_iface_map(self, node) -> dict:
        """Return {peer_id: Interface} by parsing interface names."""
        return self._peer_interface_index.peer_iface_map(node)

    def _resolve_interface(self, src_node, src_id, next_hop, iface_map) -> Interface:
        """Resolve the outgoing Interface toward next_hop."""
        return self._peer_interface_index.resolve_interface(
            src_node, src_id, next_hop, iface_map
        )

    def _install_gs_to_gs_route(self, gs_src, gs_dst, path, iface_map) -> None:
        """Install a route entry on gs_src toward gs_dst."""
        self._route_planner.install_gs_to_gs_route(gs_src, gs_dst, path, iface_map)

    def _install_sat_to_gs_route(self, sat_src, gs_dst, path, iface_map) -> None:
        """Install a route entry on sat_src toward gs_dst."""
        self._route_planner.install_sat_to_gs_route(sat_src, gs_dst, path, iface_map)

    def _entry_key(self, entry) -> tuple:
        """Return an immutable key for a RoutingEntry used in diffs."""
        return entry_key(entry)

    def _route_operation(self, op: str, entry) -> RouteOperation:
        """Build one typed route operation from a RoutingEntry."""
        return route_operation(op, entry)

    def _format_route_operation(self, op: RouteOperation) -> str:
        """Format a typed route operation for the legacy ``ip -batch`` path."""
        return format_route_operation(op)

    def _route_line(self, op: str, entry) -> str:
        """Build a single ip route command line.

        Args:
            op: The route operation, e.g. 'replace' or 'del'.
            entry: A RoutingEntry object.

        Returns:
            A string suitable for use in an ip -batch file.
        """
        return self._format_route_operation(self._route_operation(op, entry))

    def _emit_route_operations(
        self, prev_tbl, curr_tbl, live_devs: set[str] | None = None
    ) -> list[RouteOperation]:
        """Diff routing tables into minimal typed route operations."""
        return emit_route_operations(prev_tbl, curr_tbl, live_devs)

    def _emit_batch_commands(
        self, prev_tbl, curr_tbl, live_devs: set[str] | None = None
    ) -> list:
        """Legacy diff emitter returning ``ip route`` command strings."""
        return emit_batch_commands(prev_tbl, curr_tbl, live_devs)

    def _exec_batch(self, device, lines: list) -> None:
        """Execute a list of ip commands in a single batch on a container.

        Encodes the commands as base64 to avoid shell quoting issues, then
        pipes them through ip -force -batch -.

        Args:
            device: A node object with a .container attribute.
            lines: List of ip command strings (without the leading 'ip').
        """
        device.execute_command(encode_ip_batch(lines), detach=False)
