"""Static routing table planner.

Builds per-node IPv4 routing tables from a precomputed file or from
shortest-path trees computed over the topology graph (NetworkX or igraph).
The planner is pure topology/graph logic: it appends and assigns
``RoutingEntry`` objects but never talks to containers or the network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

import igraph as ig
import networkx as nx

from satgonetem.models.routing_entry import RoutingEntry
from satgonetem.routing.peer_interface_index import PeerInterfaceIndex
from satgonetem.routing.static_route_ops import route_cache_signature


class StaticRoutePlanner:
    """Build routing tables and reuse unchanged route entries."""

    def __init__(self, topology, iface_index: PeerInterfaceIndex) -> None:
        """Initialize the planner with its topology and interface index."""
        self._topology = topology
        self._iface_index = iface_index
        # Per-source next-hop snapshots and RoutingEntry objects from the last
        # successful population.  These let an update reuse every entry whose
        # route did not move instead of rebuilding all tables from scratch.
        self._source_signatures: dict[int, list] = {}
        self._entry_cache: dict[int, dict[int, RoutingEntry]] = {}
        # Source names whose last route install failed. Their unchanged
        # signatures must not be served from the cache, otherwise the diff
        # would emit zero operations and the failed install would never retry.
        self._failed_source_names: set[str] = set()

    @property
    def source_signatures(self) -> dict[int, list]:
        """Per-source next-hop snapshots from the last population."""
        return self._source_signatures

    @property
    def entry_cache(self) -> dict[int, dict[int, RoutingEntry]]:
        """Per-source cached RoutingEntry objects from the last population."""
        return self._entry_cache

    def set_failed_sources(self, names: Iterable[str]) -> None:
        """Record the source names whose last route install failed."""
        self._failed_source_names = set(names)

    def clear_reuse_cache(self) -> None:
        """Drop every cached signature and route entry."""
        self._source_signatures.clear()
        self._entry_cache.clear()

    def forget_source(self, src_id: int) -> None:
        """Drop the cached signature and entries for one source node."""
        self._source_signatures.pop(src_id, None)
        self._entry_cache.pop(src_id, None)

    def populate_tables(self, gc_disabled, load_routes) -> None:
        """Populate IPv4 routing tables for all ground stations and satellites.

        Uses either precomputed file-based routes or Dijkstra shortest-path
        depending on topology.use_file_routes.

        Args:
            gc_disabled: Context-manager factory that temporarily disables GC.
            load_routes: Callable that loads the precomputed route file.
        """
        gs_ifaces, sat_ifaces = self._iface_index.prebuild()
        gs_array = list(self._topology.get_ground_stations())
        sat_array = list(self._topology.get_satellites())

        if self._topology.use_file_routes:
            self.populate_from_file(
                gs_array, sat_array, gs_ifaces, sat_ifaces, load_routes
            )
        else:
            # The route build creates tens of thousands of short-lived path
            # lists and dict entries. They are acyclic and released by
            # reference counting, so cyclic scans during the build are pure
            # overhead and cause the 140/300 ms GC-dependent spikes.
            with gc_disabled():
                graph = self._topology.get_current_graph()
                self.populate_dijkstra_igraph(
                    graph, gs_array, sat_array, gs_ifaces, sat_ifaces
                )

    def populate_from_file(
        self, gs_array, sat_array, gs_ifaces, sat_ifaces, load_routes
    ) -> None:
        """Populate routing tables from a precomputed JSON route file.

        *load_routes* is called at populate time so callers can substitute the
        route loader without rebuilding the planner.
        """
        # File-based tables are rebuilt wholesale, so drop any Dijkstra reuse
        # state that could otherwise leak into a later update.
        self._source_signatures.clear()
        self._entry_cache.clear()
        len_path = load_routes()
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
                self.install_gs_to_gs_route(
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
                self.install_sat_to_gs_route(
                    sat_src=sat_src,
                    gs_dst=dst,
                    path=path,
                    iface_map=sat_ifaces.get(src_id),
                )

        logging.info("Routing complete: routes were loaded from the file.")

    def populate_dijkstra(
        self, graph, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables using NetworkX Dijkstra shortest paths."""
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
                self.install_gs_to_gs_route(
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
                self.install_sat_to_gs_route(
                    sat_src=sat_src,
                    gs_dst=dst,
                    path=[src_id, next_hop],
                    iface_map=src_ifaces,
                )

        logging.info("Routing complete: routes were generated using shortest path.")

    def install_source_routes(
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
        """Install or reuse the routes originating from one node.

        When the signature is unchanged the previous table is kept as both the
        previous and current table; otherwise only changed entries are rebuilt.
        A source whose last install failed always rebuilds its table so the
        next diff re-emits every route.
        """
        old_entries = self._entry_cache.get(src_id)
        if (
            signature == self._source_signatures.get(src_id)
            and old_entries is not None
            and src_name not in self._failed_source_names
        ):
            cached_table = [
                old_entries[dst_id] for dst_id in dst_ids if dst_id in old_entries
            ]
            node.ipv4_routing_table = cached_table
            node.ipv4_previous_routing_table = cached_table
            return

        cached_entries = old_entries or {}
        new_entries: dict[int, RoutingEntry] = {}
        table: list[RoutingEntry] = []
        for dst, (dst_id, destination, gateway, next_hop) in zip(
            dst_array, signature, strict=False
        ):
            if next_hop is None:
                continue
            interface = src_ifaces.get(int(next_hop))
            entry = cached_entries.get(dst_id)
            if (
                entry is None
                or entry.destination != destination
                or entry.gateway != gateway
                or entry.interface is not interface
                or entry.target_node != dst.name
                or entry.source_node != src_name
                or entry.source != src_loopback
                or entry.prefix != prefix
            ):
                entry = RoutingEntry.from_validated(
                    destination=destination,
                    gateway=gateway,
                    interface=interface,
                    prefix=prefix,
                    target_node=dst.name,
                    source_node=src_name,
                    source=src_loopback,
                )
            table.append(entry)
            new_entries[dst_id] = entry

        node.ipv4_routing_table = table
        self._entry_cache[src_id] = new_entries
        self._source_signatures[src_id] = signature

    def populate_dijkstra_igraph(
        self, graph, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables using igraph Dijkstra shortest paths.

        The NetworkX graph is converted once, then shortest-path trees are
        computed for every destination.
        """
        addressable_sats = [sat for sat in sat_array if sat.is_addressable()]
        dst_array = gs_array + addressable_sats

        ig_graph = ig.Graph.from_networkx(graph)
        nx_names = (
            ig_graph.vs["_nx_name"]
            if ig_graph.vcount() > 0 and "_nx_name" in ig_graph.vs.attributes()
            else []
        )
        name_to_idx = {name: idx for idx, name in enumerate(nx_names)}
        has_edges = ig_graph.ecount() > 0
        weights_arg = "weight" if "weight" in ig_graph.es.attributes() else None

        next_hops: dict[int, dict[int, int]] = {}
        for dst in dst_array:
            dst_id = dst.id
            if not has_edges or dst_id not in name_to_idx:
                next_hops[dst_id] = {}
                continue
            dst_idx = name_to_idx[dst_id]
            paths = ig_graph.get_shortest_paths(
                v=dst_idx,
                to=None,
                weights=weights_arg,
                output="vpath",
            )
            next_hops[dst_id] = {
                nx_names[src_idx]: nx_names[path[-2]]
                for src_idx, path in enumerate(paths)
                if src_idx != dst_idx and len(path) >= 2
            }

        # Route to the exact loopback address.  A /32 is the stable target for
        # each node and never resolves to one of its link interfaces.
        prefix = 32
        loopback_ips = {
            node.id: node.loopback.ipv4 for node in list(gs_array) + list(sat_array)
        }

        dst_ids = [dst.id for dst in dst_array]
        for gs_src in gs_array:
            src_id = gs_src.id
            src_ifaces = gs_ifaces.get(src_id)
            self.install_source_routes(
                node=gs_src,
                src_id=src_id,
                src_ifaces=src_ifaces,
                src_loopback=loopback_ips[src_id],
                src_name=gs_src.name,
                dst_array=dst_array,
                dst_ids=dst_ids,
                signature=route_cache_signature(
                    src_id, src_ifaces, dst_array, next_hops, loopback_ips
                ),
                loopback_ips=loopback_ips,
                prefix=prefix,
            )

        for sat_src in sat_array:
            src_id = sat_src.id
            src_ifaces = sat_ifaces.get(src_id)
            self.install_source_routes(
                node=sat_src,
                src_id=src_id,
                src_ifaces=src_ifaces,
                src_loopback=loopback_ips[src_id],
                src_name=sat_src.name,
                dst_array=dst_array,
                dst_ids=dst_ids,
                signature=route_cache_signature(
                    src_id, src_ifaces, dst_array, next_hops, loopback_ips
                ),
                loopback_ips=loopback_ips,
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

    def load_routes_from_file(self) -> dict:
        """Load precomputed routes from the JSON route file.

        Returns:
            Dict mapping src_id (int) to (distance_dict, path_dict) tuples
            with all keys and values coerced to int/float.
        """
        route_file = (
            f"projects/{self._topology.project_name}/"
            f"ts{self._topology.current_time_step}/routes_emulator.txt"
        )
        logging.info(f"Loading routes from {route_file}")
        with open(route_file) as f:
            raw = json.load(f)

        return {
            int(src): (
                {int(dst): float(dist) for dst, dist in dists.items()},
                {int(dst): [int(n) for n in path] for dst, path in paths.items()},
            )
            for src, (dists, paths) in raw.items()
        }

    def install_gs_to_gs_route(self, gs_src, gs_dst, path, iface_map) -> None:
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
        interface = self._iface_index.resolve_interface(
            gs_src, gs_src.id, next_hop, iface_map
        )
        if interface.peer is None:
            raise ValueError(f"Interface {interface.name} on {gs_src.name} has no peer")

        gs_src.ipv4_routing_table.append(
            RoutingEntry(
                destination=gs_dst.loopback.ipv4,
                gateway=interface.peer.ipv4,
                interface=interface,
                prefix=32,
                target_node=gs_dst.name,
                source_node=gs_src.name,
                source=gs_src.loopback.ipv4,
            )
        )

    def install_sat_to_gs_route(self, sat_src, gs_dst, path, iface_map) -> None:
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
        interface = self._iface_index.resolve_interface(
            sat_src, sat_src.id, next_hop, iface_map
        )
        if interface.peer is None:
            raise ValueError(
                f"Interface {interface.name} on {sat_src.name} has no peer"
            )

        sat_src.ipv4_routing_table.append(
            RoutingEntry(
                destination=gs_dst.loopback.ipv4,
                gateway=interface.peer.ipv4,
                interface=interface,
                prefix=32,
                target_node=gs_dst.name,
                source_node=sat_src.name,
                source=sat_src.loopback.ipv4,
            )
        )
