"""SR-MPLS route planning over a topology graph.

Builds ingress label-stack entries for every source (ground stations and
addressable satellites). The planner is pure graph/interface logic: it reads
the topology and label manager but never talks to containers, and it never
stores daemon state.
"""

from __future__ import annotations

import logging
from typing import Any

import networkx as nx

from satgonetem.models.mpls_entry import SRLabelStackEntry


class SRMPLSRoutePlanner:
    """Build SR label-stack ingress routes for every source."""

    def __init__(self, topology: Any) -> None:
        """Initialize the planner with the topology it reads nodes from."""
        self._topology = topology

    def build_all(
        self,
        graph: Any,
        label_manager: Any,
        use_php: bool,
    ) -> dict[int, list]:
        """Build SR label-stack ingress routes for every source.

        Sources and destinations are all ground stations plus addressable
        satellites. Uses a custom path when one was set via
        set_sr_custom_path, otherwise falls back to the shortest weighted
        path. All-pairs shortest paths are computed in a single pass.

        Args:
            graph: The current topology graph.
            label_manager: Manager providing custom paths and label stacks.
            use_php: Whether the destination SID is popped by PHP.

        Returns:
            Ingress entries keyed by source node ID.
        """
        gs_array = list(self._topology.get_ground_stations())
        sat_array = [
            sat for sat in list(self._topology.get_satellites()) if sat.is_addressable()
        ]
        lfib: dict[int, list] = {}
        route_count = 0

        # Pre-compute all shortest paths in one pass instead of N^2 calls.
        all_paths = dict(nx.all_pairs_dijkstra_path(graph, weight="weight"))

        # Pre-build interface lookup: {node_id: {peer_id: iface}}
        iface_lookup: dict[int, dict[int, Any]] = {}
        for node in gs_array + sat_array:
            node_ifaces: dict[int, Any] = {}
            for iface in node.interfaces:
                try:
                    peer_id = int(iface.name.split(".")[1])
                    node_ifaces[peer_id] = iface
                except (ValueError, IndexError):
                    continue
            iface_lookup[node.id] = node_ifaces

        for gs_src in gs_array + sat_array:
            src_id = gs_src.id
            if src_id not in lfib:
                lfib[src_id] = []

            src_paths = all_paths.get(src_id)
            if src_paths is None:
                continue

            src_ifaces = iface_lookup.get(src_id, {})

            for gs_dst in gs_array + sat_array:
                if src_id == gs_dst.id:
                    continue

                try:
                    custom_path = label_manager.get_custom_path(src_id, gs_dst.id)
                    path = custom_path or src_paths[gs_dst.id]

                    if len(path) < 2:
                        continue

                    first_hop_id = path[1]
                    first_iface: Any = src_ifaces.get(first_hop_id)

                    if first_iface is None or first_iface.peer is None:
                        logging.warning(
                            f"No interface from GS {src_id} to {first_hop_id}"
                        )
                        continue

                    next_hop_ip = first_iface.peer.ipv4
                    if not next_hop_ip or next_hop_ip == "0.0.0.0":
                        continue

                    label_stack = label_manager.get_label_stack_for_path(
                        path, use_php=use_php
                    )

                    entry = SRLabelStackEntry(
                        destination=gs_dst.loopback.ipv4,
                        label_stack=label_stack,
                        next_hop=next_hop_ip,
                        interface=first_iface,
                        fec_prefix=32,
                    )
                    lfib[src_id].append(entry)
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
        return lfib

    def build_for_source(
        self,
        source: Any,
        graph: Any,
        label_manager: Any,
        use_php: bool,
    ) -> list:
        """Build ingress label-stack entries for one source.

        This is the per-source rebuild used after a custom path changes. It
        keeps the original single-source ``nx.shortest_path`` algorithm and
        must not be unified with the all-pairs build.

        Args:
            source: The ground station whose routes are rebuilt.
            graph: The current topology graph.
            label_manager: Manager providing custom paths and label stacks.
            use_php: Whether the destination SID is popped by PHP.

        Returns:
            Entries for every reachable destination, in destination order.
        """
        gs_array = list(self._topology.get_ground_stations())
        sat_array = [
            sat for sat in list(self._topology.get_satellites()) if sat.is_addressable()
        ]
        new_entries: list = []

        for destination in gs_array + sat_array:
            if source.id == destination.id:
                continue
            try:
                custom_path = label_manager.get_custom_path(source.id, destination.id)
                path = (
                    custom_path
                    if custom_path
                    else nx.shortest_path(
                        graph, source=source.id, target=destination.id, weight="weight"
                    )
                )

                if len(path) < 2:
                    continue

                first_hop_id = path[1]
                iface = self._topology._get_interface_to_peer(source, first_hop_id)
                if iface is None or iface.peer is None:
                    continue

                next_hop_ip = iface.peer.ipv4
                if not next_hop_ip or next_hop_ip == "0.0.0.0":
                    continue

                label_stack = label_manager.get_label_stack_for_path(
                    path, use_php=use_php
                )

                entry = SRLabelStackEntry(
                    destination=destination.loopback.ipv4,
                    label_stack=label_stack,
                    next_hop=next_hop_ip,
                    interface=iface,
                    fec_prefix=32,
                )
                new_entries.append(entry)

            except Exception as e:
                logging.error(
                    f"Failed to build SR route {source.id} -> {destination.id}: {e}"
                )

        return new_entries
