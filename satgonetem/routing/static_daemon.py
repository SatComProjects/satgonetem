"""
Static IP routing daemon.

Computes shortest paths via Dijkstra (or loads from a file) and installs them
on every node. All routing logic is self-contained; the topology manager is
only used to access node/graph data.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, List, Optional

import networkx as nx

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.routing_entry import RoutingEntry
from satgonetem.models.satellite import Satellite
from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.utils.utils import get_interface_from_name

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


class StaticRoutingDaemon(RoutingDaemon):
    """Static IP routing daemon.

    On init, computes shortest paths via Dijkstra (or loads from a pre-computed
    file) and installs IP routes on every satellite and ground station. On
    update, recomputes and replaces routes after topology changes.
    """

    def init(self, max_workers: int = 4) -> bool:
        """Initialize static IP routing.

        Populates routing tables using shortest paths, clears stale previous
        tables, then applies the computed routes to all nodes in parallel.

        Args:
            max_workers: Maximum number of worker threads for parallel route
                installation.

        Returns:
            True if routing was installed successfully, False otherwise.
        """
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

    def update(self, new_links: "List[Link]", max_workers: int = 4) -> None:
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

    def remove(self, node: "Optional[Node]" = None, max_workers: int = 4) -> None:
        """Remove all installed static IP routes.

        Deletes routes from the container and clears the in-memory routing
        tables. If node is provided, only that node is affected; otherwise
        all satellites and ground stations are processed in parallel.

        Args:
            node: Target node. If None, all nodes in the topology are cleared.
            max_workers: Maximum number of worker threads for parallel removal.
        """
        if node is not None:
            self._remove_node_routes(node)
            return

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
        graph = self.topology.get_current_graph()
        gs_ifaces, sat_ifaces = self._prebuild_iface_maps()
        gs_array = list(self.topology.get_ground_stations())
        sat_array = list(self.topology.get_satellites())

        if self.topology.use_file_routes:
            self._populate_from_file(gs_array, sat_array, gs_ifaces, sat_ifaces)
        else:
            self._populate_dijkstra(graph, gs_array, sat_array, gs_ifaces, sat_ifaces)

    def _apply_satellite_routes(self, max_workers: int = 4) -> None:
        """Apply satellite IPv4 route changes to containers using a diff.

        Args:
            max_workers: Maximum number of worker threads.
        """
        jobs = [
            (sat, lines)
            for sat in self.topology.satellites.values()
            if (
                lines := self._emit_batch_commands(
                    sat.ipv4_previous_routing_table, sat.ipv4_routing_table
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

    def _apply_ground_station_routes(self, max_workers: int = 4) -> None:
        """Apply ground-station IPv4 route changes to containers using a diff.

        Args:
            max_workers: Maximum number of worker threads.
        """
        jobs = [
            (gs, lines)
            for gs in self.topology.ground_stations.values()
            if (
                lines := self._emit_batch_commands(
                    gs.ipv4_previous_routing_table, gs.ipv4_routing_table
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
        """Rotate current table to previous and clear current for all nodes."""
        for node in list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        ):
            node.ipv4_previous_routing_table = node.ipv4_routing_table.copy()
            node.ipv4_routing_table.clear()

    def _populate_from_file(
        self, gs_array, sat_array, gs_ifaces, sat_ifaces
    ) -> None:
        """Populate routing tables from a precomputed JSON route file.

        Args:
            gs_array: List of ground station nodes.
            sat_array: List of satellite nodes.
            gs_ifaces: Prebuilt peer_id->Interface maps per GS.
            sat_ifaces: Prebuilt peer_id->Interface maps per satellite.
        """
        len_path = self._load_routes_from_file()

        for gs_src in gs_array:
            src_id = gs_src.id
            path_dict = len_path.get(src_id, (None, {}))[1]
            for gs_dst in gs_array:
                if gs_dst is gs_src:
                    continue
                path = path_dict.get(gs_dst.id)
                if path is None:
                    continue
                self._install_gs_to_gs_route(
                    gs_src=gs_src,
                    gs_dst=gs_dst,
                    path=path,
                    iface_map=gs_ifaces.get(src_id),
                )

        for sat_src in sat_array:
            src_id = sat_src.id
            path_dict = len_path.get(src_id, (None, {}))[1]
            for gs_dst in gs_array:
                path = path_dict.get(gs_dst.id)
                if path is None:
                    continue
                self._install_sat_to_gs_route(
                    sat_src=sat_src,
                    gs_dst=gs_dst,
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
        next_hops_to_gs: dict[int, dict[int, int]] = {}
        for gs_dst in gs_array:
            dst_id = gs_dst.id
            pred, _ = nx.dijkstra_predecessor_and_distance(
                graph, dst_id, weight="weight"
            )
            next_hops_to_gs[dst_id] = {
                src_id: preds[0]
                for src_id, preds in pred.items()
                if src_id != dst_id and preds
            }

        for gs_src in gs_array:
            src_id = gs_src.id
            src_ifaces = gs_ifaces.get(src_id)
            for gs_dst in gs_array:
                if gs_dst is gs_src:
                    continue
                next_hop = next_hops_to_gs.get(gs_dst.id, {}).get(src_id)
                if next_hop is None:
                    continue
                self._install_gs_to_gs_route(
                    gs_src=gs_src,
                    gs_dst=gs_dst,
                    path=[src_id, next_hop],
                    iface_map=src_ifaces,
                )

        for sat_src in sat_array:
            src_id = sat_src.id
            src_ifaces = sat_ifaces.get(src_id)
            for gs_dst in gs_array:
                next_hop = next_hops_to_gs.get(gs_dst.id, {}).get(src_id)
                if next_hop is None:
                    continue
                self._install_sat_to_gs_route(
                    sat_src=sat_src,
                    gs_dst=gs_dst,
                    path=[src_id, next_hop],
                    iface_map=src_ifaces,
                )

        logging.info("Routing complete: routes were generated using shortest path.")

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

        Returns:
            Tuple of (gs_ifaces, sat_ifaces) where each value is a dict
            mapping node_id to a {peer_id: Interface} dict.
        """
        gs_ifaces = {
            gs.id: self._peer_iface_map(gs)
            for gs in self.topology.ground_stations.values()
        }
        sat_ifaces = {
            sat.id: self._peer_iface_map(sat)
            for sat in self.topology.satellites.values()
        }
        return gs_ifaces, sat_ifaces

    def _peer_iface_map(self, node) -> dict:
        """Return {peer_id: Interface} by parsing interface names.

        Args:
            node: A satellite or ground station node.

        Returns:
            Dict mapping integer peer_id to Interface objects.
        """
        pattern = re.compile(r"^(Gnd|Sat)\d+\.\d+$")
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
        """Install a GS->GS route entry on gs_src toward gs_dst.

        Args:
            gs_src: Source ground station node.
            gs_dst: Destination ground station node.
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
        """Install a SAT->GS route entry on sat_src toward gs_dst.

        Args:
            sat_src: Source satellite node.
            gs_dst: Destination ground station node.
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

    def _emit_batch_commands(self, prev_tbl, curr_tbl) -> list:
        """Diff prev_tbl vs curr_tbl and return minimal ip route replace commands.

        Args:
            prev_tbl: Previous routing table (list of RoutingEntry).
            curr_tbl: Current routing table (list of RoutingEntry).

        Returns:
            List of ip route command strings for new routes only.
        """
        curr = {self._entry_key(e): e for e in curr_tbl}
        prev = {self._entry_key(e): e for e in prev_tbl}
        return [
            self._route_line("replace", e)
            for k, e in curr.items()
            if k not in prev
        ]

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
        cmd = f'bash -lc "echo {b64} | base64 -d | ip -force -batch -"'
        device.container.exec_run(cmd=cmd, detach=True)
