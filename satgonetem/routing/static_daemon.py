"""
Static IP routing daemon.

Computes shortest paths via Dijkstra (or loads from a file) and installs them
on every node. All routing logic is self-contained; the topology manager is
only used to access node/graph data.
"""

from __future__ import annotations

import base64
import gc
import json
import logging
import re
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, List, Optional

import igraph as ig
import networkx as nx

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.routing_entry import RoutingEntry
from satgonetem.models.satellite import Satellite
from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.utils.ip_utils import IPUtils
from satgonetem.utils.utils import get_interface_from_name, time_

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node
    from satgonetem.services.topology_satcom import TopologyManager


_PEER_IFACE_PATTERN = re.compile(r"^(Gnd|Sat)\d+\.\d+$")


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

    def __init__(self, topology: "TopologyManager") -> None:
        """Initialize the daemon and its route-reuse caches.

        Args:
            topology: The topology manager that owns this daemon.
        """
        super().__init__(topology)
        # Per-source next-hop snapshots and RoutingEntry objects from the last
        # successful population.  These let an update reuse every entry whose
        # route did not move instead of rebuilding all tables from scratch.
        self._source_signatures: dict[int, list] = {}
        self._entry_cache: dict[int, dict[int, RoutingEntry]] = {}
        self._iface_cache_version: int | None = None
        self._iface_cache = None

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
        self._source_signatures.clear()
        self._entry_cache.clear()
        self._populate_routing_tables()
        try:
            for node in list(self.topology.get_satellites()) + list(
                self.topology.get_ground_stations()
            ):
                node.ipv4_previous_routing_table.clear()

            self._apply_ground_station_routes(max_workers=max_workers)
            self._apply_satellite_routes(max_workers=max_workers)
            return True
        except Exception as e:
            logging.error(f"Error initializing static IP routing: {e}")
            return False

    def update(self, new_links: "List[Link]", max_workers: int = 128) -> None:
        """Update static IP routing after topology changes.

        Recomputes shortest-path routing tables and pushes incremental route
        changes to all nodes. The new_links argument is accepted for interface
        consistency but is not used by static routing.

        Args:
            new_links: Newly added links (unused for static routing).
            max_workers: Maximum number of worker threads for parallel route
                updates.
        """
        self._populate_routing_tables()
        if self.topology.status:
            self._apply_ground_station_routes(max_workers=max_workers)
            self._apply_satellite_routes(max_workers=max_workers)
        else:
            logging.warning("Static routing update skipped: topology not yet active")

    def remove(self, node: "Optional[Node]" = None, max_workers: int = 128) -> None:
        """Remove all installed static IP routes.

        Deletes routes from the container and clears the in-memory routing
        tables. If node is provided, only that node is affected; otherwise
        all satellites and ground stations are processed in parallel.

        Args:
            node: Target node. If None, all nodes in the topology are cleared.
            max_workers: Maximum number of worker threads for parallel removal.
        """
        if node is not None:
            self._source_signatures.pop(node.id, None)
            self._entry_cache.pop(node.id, None)
            self._remove_node_routes(node)
            return

        self._source_signatures.clear()
        self._entry_cache.clear()
        nodes = list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        )
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._remove_node_routes, n): n for n in nodes}
            for fut in as_completed(futures):
                n = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"Failed to remove routes on {n.name}: {e}")

    def _populate_routing_tables(self) -> None:
        """Populate IPv4 routing tables for all ground stations and satellites.

        Uses either precomputed file-based routes or Dijkstra shortest-path
        depending on topology.use_file_routes.
        """
        self._equalize_routing_tables()
        gs_ifaces, sat_ifaces = self._prebuild_iface_maps()
        gs_array = list(self.topology.get_ground_stations())
        sat_array = list(self.topology.get_satellites())

        if self.topology.use_file_routes:
            self._populate_from_file(gs_array, sat_array, gs_ifaces, sat_ifaces)
        else:
            # The route build creates tens of thousands of short-lived path
            # lists and dict entries. They are acyclic and released by
            # reference counting, so cyclic scans during the build are pure
            # overhead and cause the 140/300 ms GC-dependent spikes.
            with _gc_disabled():
                graph = self.topology.get_current_graph()
                self._populate_dijkstra_igraph(
                    graph, gs_array, sat_array, gs_ifaces, sat_ifaces
                )

    def _apply_satellite_routes(self, max_workers: int = 128) -> None:
        """Apply satellite IPv4 route changes to containers using a diff.

        Args:
            max_workers: Maximum number of worker threads.
        """
        jobs = [
            (sat, lines)
            for sat in self.topology.satellites.values()
            if (
                lines := self._emit_batch_commands(
                    sat.ipv4_previous_routing_table,
                    sat.ipv4_routing_table,
                    live_devs={iface.get_iname() for iface in sat.interfaces},
                )
            )
        ]

        if not jobs:
            logging.info("No satellite route changes needed.")
            return

        submitted = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(self._exec_batch, sat, lines): sat for sat, lines in jobs
            }
            for fut in as_completed(future_map):
                sat = future_map[fut]
                submitted += 1
                try:
                    fut.result()
                except Exception as e:
                    errors += 1
                    logging.exception(f"Route update failed on {sat.name}: {e}")
                logging.debug(
                    f"Route updates: {submitted}/{len(jobs)} (errors: {errors})"
                )
        logging.info(
            f"Satellite route updates complete. ok={len(jobs) - errors}, error={errors}"
        )

    def _apply_ground_station_routes(self, max_workers: int = 128) -> None:
        """Apply ground-station IPv4 route changes to containers using a diff.

        Args:
            max_workers: Maximum number of worker threads.
        """
        jobs = [
            (gs, lines)
            for gs in self.topology.ground_stations.values()
            if (
                lines := self._emit_batch_commands(
                    gs.ipv4_previous_routing_table,
                    gs.ipv4_routing_table,
                    live_devs={iface.get_iname() for iface in gs.interfaces},
                )
            )
        ]

        if not jobs:
            logging.info("No ground station route changes needed.")
            return

        submitted = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(self._exec_batch, gs, lines): gs for gs, lines in jobs
            }
            for fut in as_completed(future_map):
                gs = future_map[fut]
                submitted += 1
                try:
                    fut.result()
                except Exception as e:
                    errors += 1
                    logging.exception(f"Route update failed on {gs.name}: {e}")
                logging.debug(
                    f"Route updates: {submitted}/{len(jobs)} (errors: {errors})"
                )
        logging.info(
            f"GS route updates complete. ok={len(jobs) - errors}, error={errors}"
        )

    def _remove_node_routes(self, node: "Node") -> None:
        """Delete all IPv4 routes from a single node.

        Builds ip route del commands for every entry in the node's current
        routing table, executes them in a single batch, then clears both the
        current and previous in-memory tables.

        Args:
            node: The node whose routes should be removed.
        """
        lines = [
            f"route del {entry.destination}{entry.get_prefix()}"
            f" via {entry.gateway} dev {entry.interface.get_iname()}"
            for entry in node.ipv4_routing_table
        ]
        if lines:
            self._exec_batch(node, lines)
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
            node.ipv4_previous_routing_table = node.ipv4_routing_table
            node.ipv4_routing_table = []

    def _populate_from_file(self, gs_array, sat_array, gs_ifaces, sat_ifaces) -> None:
        """Populate routing tables from a precomputed JSON route file.

        Args:
            gs_array: List of ground station nodes.
            sat_array: List of satellite nodes.
            gs_ifaces: Prebuilt peer_id->Interface maps per GS.
            sat_ifaces: Prebuilt peer_id->Interface maps per satellite.
        """
        # File-based tables are rebuilt wholesale, so drop any Dijkstra reuse
        # state that could otherwise leak into a later update.
        self._source_signatures.clear()
        self._entry_cache.clear()
        len_path = self._load_routes_from_file()
        addressable_sats = [sat for sat in sat_array if sat.is_addressable()]
        dst_array = gs_array + addressable_sats

        for gs_src in gs_array:
            src_id = gs_src.id
            path_dict = len_path.get(src_id, (None, {}))[1]
            for dst in dst_array:
                if dst is gs_src:
                    continue
                path = path_dict.get(dst.id)
                if path is None:
                    continue
                self._install_gs_to_gs_route(
                    gs_src=gs_src,
                    gs_dst=dst,
                    path=path,
                    iface_map=gs_ifaces.get(src_id),
                )

        for sat_src in sat_array:
            src_id = sat_src.id
            path_dict = len_path.get(src_id, (None, {}))[1]
            for dst in dst_array:
                if dst is sat_src:
                    continue
                path = path_dict.get(dst.id)
                if path is None:
                    continue
                self._install_sat_to_gs_route(
                    sat_src=sat_src,
                    gs_dst=dst,
                    path=path,
                    iface_map=sat_ifaces.get(src_id),
                )

        logging.info("Routing complete: routes were loaded from the file.")

    def _populate_dijkstra(
        self, graph, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables using Dijkstra shortest paths.

        Args:
            graph: The current networkx topology graph.
            gs_array: List of ground station nodes.
            sat_array: List of satellite nodes.
            gs_ifaces: Prebuilt peer_id->Interface maps per GS.
            sat_ifaces: Prebuilt peer_id->Interface maps per satellite.
        """
        addressable_sats = [sat for sat in sat_array if sat.is_addressable()]
        dst_array = gs_array + addressable_sats

        next_hops: dict[int, dict[int, int]] = {}
        for dst in dst_array:
            dst_id = dst.id
            pred, _ = nx.dijkstra_predecessor_and_distance(
                graph, dst_id, weight="weight"
            )
            next_hops[dst_id] = {
                src_id: preds[0]
                for src_id, preds in pred.items()
                if src_id != dst_id and preds
            }

        for gs_src in gs_array:
            src_id = gs_src.id
            src_ifaces = gs_ifaces.get(src_id)
            for dst in dst_array:
                if dst.id == src_id:
                    continue
                next_hop = next_hops.get(dst.id, {}).get(src_id)
                if next_hop is None:
                    continue
                self._install_gs_to_gs_route(
                    gs_src=gs_src,
                    gs_dst=dst,
                    path=[src_id, next_hop],
                    iface_map=src_ifaces,
                )

        for sat_src in sat_array:
            src_id = sat_src.id
            src_ifaces = sat_ifaces.get(src_id)
            for dst in dst_array:
                if dst.id == src_id:
                    continue
                next_hop = next_hops.get(dst.id, {}).get(src_id)
                if next_hop is None:
                    continue
                self._install_sat_to_gs_route(
                    sat_src=sat_src,
                    gs_dst=dst,
                    path=[src_id, next_hop],
                    iface_map=src_ifaces,
                )

        logging.info("Routing complete: routes were generated using shortest path.")

    def _install_source_routes(
        self,
        node,
        src_id: int,
        src_ifaces,
        src_summary: str,
        src_name: str,
        dst_array,
        dst_ids: list[int],
        signature: list,
        summarized_ips: dict[int, str],
        prefix: int,
    ) -> None:
        """Install or reuse the routes originating from one node.

        When the node's next-hop vector is unchanged the previous table is kept
        as both the previous and current table, allowing the route diff to
        short-circuit.  Otherwise only entries whose destination, gateway or
        interface changed are allocated; unchanged entries are reused.

        Args:
            node: Source node whose ``ipv4_routing_table`` is populated.
            src_id: Integer ID of the source node.
            src_ifaces: Mapping of peer node ID to outgoing Interface.
            src_summary: Summarized loopback IP of the source.
            src_name: Name of the source node.
            dst_array: Ordered list of destination nodes.
            dst_ids: IDs of ``dst_array`` in the same order.
            signature: Next-hop IDs for this source, in ``dst_array`` order.
            summarized_ips: Mapping of node ID to summarized destination IP.
            prefix: Prefix length used for all installed routes.
        """
        old_entries = self._entry_cache.get(src_id)
        if (
            signature == self._source_signatures.get(src_id)
            and old_entries is not None
        ):
            table = [
                old_entries[dst_id] for dst_id in dst_ids if dst_id in old_entries
            ]
            node.ipv4_routing_table = table
            node.ipv4_previous_routing_table = table
            return

        cached_entries = old_entries or {}
        new_entries: dict[int, RoutingEntry] = {}
        table: list[RoutingEntry] = []
        for dst, next_hop in zip(dst_array, signature):
            if next_hop is None:
                continue
            dst_id = dst.id
            interface = src_ifaces.get(int(next_hop))
            gateway = interface.peer.ipv4
            destination = summarized_ips[dst_id]
            entry = cached_entries.get(dst_id)
            if (
                entry is None
                or entry.destination != destination
                or entry.gateway != gateway
                or entry.interface is not interface
                or entry.target_node != dst.name
                or entry.source_node != src_name
                or entry.source != src_summary
                or entry.prefix != prefix
            ):
                entry = RoutingEntry.from_validated(
                    destination=destination,
                    gateway=gateway,
                    interface=interface,
                    prefix=prefix,
                    target_node=dst.name,
                    source_node=src_name,
                    source=src_summary,
                )
            table.append(entry)
            new_entries[dst_id] = entry

        node.ipv4_routing_table = table
        self._entry_cache[src_id] = new_entries
        self._source_signatures[src_id] = signature

    def _populate_dijkstra_igraph(
        self, graph, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables using Dijkstra shortest paths via igraph.

        This is the igraph-backed equivalent of _populate_dijkstra. The NetworkX
        graph is converted to an igraph.Graph once, then shortest-path trees are
        computed for every destination.

        Args:
            graph: The current networkx topology graph.
            gs_array: List of ground station nodes.
            sat_array: List of satellite nodes.
            gs_ifaces: Prebuilt peer_id->Interface maps per GS.
            sat_ifaces: Prebuilt peer_id->Interface maps per satellite.
        """
        addressable_sats = [sat for sat in sat_array if sat.is_addressable()]
        dst_array = gs_array + addressable_sats

        ig_graph = ig.Graph.from_networkx(graph)
        nx_names = ig_graph.vs["_nx_name"]
        name_to_idx = {name: idx for idx, name in enumerate(nx_names)}

        next_hops: dict[int, dict[int, int]] = {}
        for dst in dst_array:
            dst_id = dst.id
            dst_idx = name_to_idx[dst_id]
            paths = ig_graph.get_shortest_paths(
                v=dst_idx,
                to=None,
                weights="weight",
                output="vpath",
            )
            next_hops[dst_id] = {
                nx_names[src_idx]: nx_names[path[-2]]
                for src_idx, path in enumerate(paths)
                if src_idx != dst_idx and len(path) >= 2
            }

        # Transpose the destination-indexed next-hop map into one vector per
        # source.  Each vector doubles as the cache signature for that source.
        next_hop_by_src: dict[int, list] = {}
        for dst_idx, dst in enumerate(dst_array):
            for src_id, next_hop in next_hops[dst.id].items():
                signature = next_hop_by_src.get(src_id)
                if signature is None:
                    signature = [None] * len(dst_array)
                    next_hop_by_src[src_id] = signature
                signature[dst_idx] = next_hop
        no_route_signature = [None] * len(dst_array)

        # Pre-summarize loopback IPs once.  Routes are built with prefix 15,
        # and the inputs are generated by this daemon, so we can use the fast
        # path that skips per-entry IPv4 validation.
        prefix = 15
        summarized_ips = {
            node.id: IPUtils.summarize_ipv4_address(node.loopback.ipv4, prefix)
            for node in list(gs_array) + list(sat_array)
        }

        dst_ids = [dst.id for dst in dst_array]
        for gs_src in gs_array:
            src_id = gs_src.id
            self._install_source_routes(
                node=gs_src,
                src_id=src_id,
                src_ifaces=gs_ifaces.get(src_id),
                src_summary=summarized_ips[src_id],
                src_name=gs_src.name,
                dst_array=dst_array,
                dst_ids=dst_ids,
                signature=next_hop_by_src.get(src_id, no_route_signature),
                summarized_ips=summarized_ips,
                prefix=prefix,
            )

        for sat_src in sat_array:
            src_id = sat_src.id
            self._install_source_routes(
                node=sat_src,
                src_id=src_id,
                src_ifaces=sat_ifaces.get(src_id),
                src_summary=summarized_ips[src_id],
                src_name=sat_src.name,
                dst_array=dst_array,
                dst_ids=dst_ids,
                signature=next_hop_by_src.get(src_id, no_route_signature),
                summarized_ips=summarized_ips,
                prefix=prefix,
            )

        active_source_ids = {node.id for node in gs_array}
        active_source_ids.update(sat.id for sat in sat_array)
        self._source_signatures = {
            src_id: signature
            for src_id, signature in self._source_signatures.items()
            if src_id in active_source_ids
        }
        self._entry_cache = {
            src_id: entries
            for src_id, entries in self._entry_cache.items()
            if src_id in active_source_ids
        }

        logging.info(
            "Routing complete: routes were generated using shortest path (igraph)."
        )

    def _load_routes_from_file(self) -> dict:
        """Load precomputed routes from the JSON route file.

        Returns:
            Dict mapping src_id (int) to (distance_dict, path_dict) tuples
            with all keys and values coerced to int/float.
        """
        route_file = (
            f"projects/{self.topology.project_name}/"
            f"ts{self.topology.current_time_step}/routes_emulator.txt"
        )
        logging.info(f"Loading routes from {route_file}")
        with open(route_file, "r") as f:
            raw = json.load(f)

        return {
            int(src): (
                {int(dst): float(dist) for dst, dist in dists.items()},
                {int(dst): [int(n) for n in path] for dst, path in paths.items()},
            )
            for src, (dists, paths) in raw.items()
        }

    def _prebuild_iface_maps(self):
        """Build fast peer_id->Interface lookups for every node.

        The maps are cached by the topology's interface version and rebuilt
        only when link interfaces are added or removed.

        Returns:
            Tuple of (gs_ifaces, sat_ifaces) where each value is a dict
            mapping node_id to a {peer_id: Interface} dict.
        """
        version = getattr(self.topology, "interface_version", None)
        if (
            isinstance(version, int)
            and self._iface_cache is not None
            and self._iface_cache_version == version
        ):
            return self._iface_cache

        gs_ifaces = {
            gs.id: self._peer_iface_map(gs)
            for gs in self.topology.ground_stations.values()
        }
        sat_ifaces = {
            sat.id: self._peer_iface_map(sat)
            for sat in self.topology.satellites.values()
        }
        result = (gs_ifaces, sat_ifaces)
        if isinstance(version, int):
            self._iface_cache = result
            self._iface_cache_version = version
        return result

    def _peer_iface_map(self, node) -> dict:
        """Return {peer_id: Interface} by parsing interface names.

        Args:
            node: A satellite or ground station node.

        Returns:
            Dict mapping integer peer_id to Interface objects.
        """
        pattern = _PEER_IFACE_PATTERN
        mapping = {}
        for iface in node.interfaces:
            name = str(iface.name)
            if not pattern.match(name):
                logging.warning(
                    "Interface '%s' on node '%s' does not match expected pattern, skipping.",
                    name,
                    getattr(node, "_id", repr(node)),
                )
                continue
            try:
                _, peer = name.split(".", 1)
                mapping[int(peer)] = iface
            except (ValueError, TypeError):
                continue
        return mapping

    def _resolve_interface(self, src_node, src_id, next_hop, iface_map) -> "Interface":
        """Resolve the outgoing Interface toward next_hop.

        Tries the prebuilt iface_map first, then falls back to name-based lookup.

        Args:
            src_node: The source node object.
            src_id: Integer ID of the source node.
            next_hop: Integer ID of the next-hop node.
            iface_map: {peer_id: Interface} for fast lookup, or None.

        Returns:
            The resolved Interface.

        Raises:
            ValueError: If no interface toward next_hop can be found.
        """
        interface = iface_map.get(int(next_hop)) if iface_map is not None else None
        if interface is None:
            match self.topology.satellites.get(int(next_hop)):
                case Satellite() as peer:
                    interface = get_interface_from_name(
                        src_node.interfaces, f"{src_id}.{peer.id}"
                    )
                case None:
                    match self.topology.ground_stations.get(int(next_hop)):
                        case GroundStation() as peer:
                            interface = get_interface_from_name(
                                src_node.interfaces, f"{src_id}.{peer.id}"
                            )
                        case None:
                            pass
                        case _ as unexpected:
                            raise TypeError(
                                f"Expected GroundStation, got {type(unexpected)}"
                            )
                case _ as unexpected:
                    raise TypeError(f"Expected Satellite, got {type(unexpected)}")
        if interface is None:
            raise ValueError(
                f"Could not resolve interface from {src_node.name} (id={src_id}) "
                f"to next hop id={next_hop}"
            )
        return interface

    def _install_gs_to_gs_route(self, gs_src, gs_dst, path, iface_map) -> None:
        """Install a route entry on gs_src toward gs_dst.

        Args:
            gs_src: Source ground station node.
            gs_dst: Destination node (ground station or addressable satellite).
            path: List of node IDs [src_id, next_hop, ...].
            iface_map: {peer_id: Interface} for fast lookup.
        """
        if len(path) < 2:
            logging.warning(f"Invalid path from {gs_src.id} to {gs_dst.id}: {path}")
            return

        next_hop = path[1]
        interface = self._resolve_interface(gs_src, gs_src.id, next_hop, iface_map)

        gs_src.ipv4_routing_table.append(
            RoutingEntry(
                destination=gs_dst.loopback.ipv4,
                gateway=interface.peer.ipv4,
                interface=interface,
                prefix=15,
                target_node=gs_dst.name,
                source_node=gs_src.name,
                source=gs_src.loopback.ipv4,
            )
        )

    def _install_sat_to_gs_route(self, sat_src, gs_dst, path, iface_map) -> None:
        """Install a route entry on sat_src toward gs_dst.

        Args:
            sat_src: Source satellite node.
            gs_dst: Destination node (ground station or addressable satellite).
            path: List of node IDs [src_id, next_hop, ...].
            iface_map: {peer_id: Interface} for fast lookup.
        """
        if len(path) < 2:
            return

        next_hop = path[1]
        interface = self._resolve_interface(sat_src, sat_src.id, next_hop, iface_map)

        sat_src.ipv4_routing_table.append(
            RoutingEntry(
                destination=gs_dst.loopback.ipv4,
                gateway=interface.peer.ipv4,
                interface=interface,
                prefix=15,
                target_node=gs_dst.name,
                source_node=sat_src.name,
                source=sat_src.loopback.ipv4,
            )
        )

    def _entry_key(self, entry) -> tuple:
        """Return an immutable key for a RoutingEntry used in diffs.

        Args:
            entry: A RoutingEntry object.

        Returns:
            Tuple of (destination, prefix, gateway, iface_name).
        """
        return (
            entry.destination,
            entry.get_prefix(),
            entry.gateway,
            entry.interface.get_iname(),
        )

    def _route_line(self, op: str, entry) -> str:
        """Build a single ip route command line.

        Args:
            op: The route operation, e.g. 'replace' or 'del'.
            entry: A RoutingEntry object.

        Returns:
            A string suitable for use in an ip -batch file.
        """
        addr = entry.destination + entry.get_prefix()
        dev = entry.interface.get_iname()
        return f"route {op} {addr} via {entry.gateway} dev {dev}"

    def _emit_batch_commands(
        self, prev_tbl, curr_tbl, live_devs: Optional[set[str]] = None
    ) -> list:
        """Diff prev_tbl vs curr_tbl and return minimal ip route commands.

        A destination prefix that disappeared entirely must be deleted
        explicitly; otherwise the kernel keeps forwarding traffic to a next
        hop that is no longer part of the topology. If the destination prefix
        still exists with a different gateway/interface, ``ip route replace``
        updates it in place and a preceding delete is redundant.

        Deletions are skipped when the entry's outgoing device is no longer
        present on the node. Removing a veth makes the kernel purge every
        route that referenced it, so issuing ``ip route del ... dev <gone>``
        is guaranteed to fail with "Cannot find device" and would mark an
        otherwise successful batch as failed.

        Args:
            prev_tbl: Previous routing table (list of RoutingEntry).
            curr_tbl: Current routing table (list of RoutingEntry).
            live_devs: Device names currently present on the node. When
                provided, stale entries whose device is absent are not
                deleted. When None, every removed entry is deleted.

        Returns:
            List of ip route command strings: deletions first, then
            additions/replacements.
        """
        if prev_tbl is curr_tbl:
            return []

        curr = {self._entry_key(e): e for e in curr_tbl}
        prev = {self._entry_key(e): e for e in prev_tbl}
        curr_prefixes = {(e.destination, e.get_prefix()) for e in curr_tbl}
        removed = [
            self._route_line("del", e)
            for k, e in prev.items()
            if k not in curr
            and (e.destination, e.get_prefix()) not in curr_prefixes
            and (live_devs is None or e.interface.get_iname() in live_devs)
        ]
        added = [
            self._route_line("replace", e) for k, e in curr.items() if k not in prev
        ]
        return removed + added

    def _exec_batch(self, device, lines: list) -> None:
        """Execute a list of ip commands in a single batch on a container.

        Encodes the commands as base64 to avoid shell quoting issues, then
        pipes them through ip -force -batch -.

        Args:
            device: A node object with a .container attribute.
            lines: List of ip command strings (without the leading 'ip').
        """
        payload = "\n".join(lines)
        b64 = base64.b64encode(payload.encode()).decode()
        cmd = f"echo {b64} | base64 -d | ip -force -batch -"
        device.execute_command(cmd, detach=False)
