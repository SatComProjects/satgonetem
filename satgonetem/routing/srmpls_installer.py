"""SR-MPLS route installation on ground stations and satellites.

Extracted from ``SRMPLSDaemon``. The installer owns MPLS enablement and the
three incremental apply passes (satellite SID/forward rules, ground-station
ingress routes, satellite label-stack routes) plus the single-source install
triggered by a custom-path change.

The per-pass route caches are passed by reference and mutated in place. The
daemon keeps object identity for them, and tests assign them directly, so this
module must never rebind a cache.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.mpls_entry import SRForwardEntry, SRNodeSIDEntry
from satgonetem.models.satellite import Satellite
from satgonetem.routing.commands import enable_mpls as _enable_mpls_helper
from satgonetem.routing.srmpls_route_ops import (
    build_batch,
    cached_route_keys,
    route_changes,
)


class SRMPLSRouteInstaller:
    """Install SR-MPLS forwarding entries on every node."""

    def __init__(self, topology: Any) -> None:
        """Initialize the installer with the topology it installs onto."""
        self._topology = topology

    def enable_mpls(self, node: Any) -> None:
        """Enable MPLS forwarding on a node."""
        _enable_mpls_helper(node, load_modules=False)

    def enable_mpls_on_all_nodes(
        self,
        max_workers: int,
        enable_mpls: Callable[[Any], None],
    ) -> int:
        """Enable MPLS forwarding on all satellites and ground stations.

        Args:
            max_workers: Maximum number of parallel worker threads.
            enable_mpls: Per-node enable callable, resolved by the caller so a
                daemon-level patch still intercepts every node.

        Returns:
            Number of nodes on which MPLS could not be enabled.
        """
        nodes = list(self._topology.satellites.values()) + list(
            self._topology.ground_stations.values()
        )
        if not nodes:
            return 0

        total_nodes = len(nodes)
        tic = time.perf_counter()
        workers = min(max_workers, total_nodes)
        ok = 0
        error = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(enable_mpls, node): node for node in nodes if node.container
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
        return error

    def apply_sr_node_sids_to_satellites(
        self,
        label_manager: Any,
        sr_lfib: dict[int, list],
        prev_node_sid_routes: dict[int, list[str]],
        prev_sat_routes: dict[int, list[str]],
    ) -> int:
        """Install SR-MPLS forwarding entries on all satellites.

        This pass owns the satellite's own Node SID, its per-neighbour forward
        rules, and the plain IPv4 fallback routes to directly connected ground
        stations. A ground-station prefix that the label-stack pass also
        installs is left to that pass, so a route has exactly one owner and
        this pass never deletes a prefix the label-stack pass still holds.

        On the first call (previous table empty) all entries are installed.
        On subsequent calls only entries whose attributes changed are sent,
        and entries no longer present are deleted.

        Args:
            label_manager: Manager providing Node SIDs.
            sr_lfib: Ingress entries keyed by source node ID.
            prev_node_sid_routes: This pass's cache, mutated in place.
            prev_sat_routes: The stack pass's cache, read for ownership.

        Returns:
            Number of satellites whose forwarding entries could not be installed.
        """
        all_sats = list(self._topology.satellites.values())
        all_gs = list(self._topology.ground_stations.values())
        if not all_sats:
            return 0
        workers = min(32, (os.cpu_count() or 4) * 4, len(all_sats))

        def install_sr_forwarding(sat: Satellite) -> bool:
            my_sid = label_manager.get_node_sid(sat.id)
            if my_sid is None:
                return True

            stack_commands = [
                entry.to_iproute2_command() for entry in sr_lfib.get(sat.id, [])
            ]
            stack_keys = cached_route_keys(stack_commands)

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

                neighbor_sid = label_manager.get_node_sid(neighbor_id)
                if neighbor_sid is None:
                    continue

                next_hop_ip = iface.peer.ipv4
                if not next_hop_ip or next_hop_ip == "0.0.0.0":
                    continue

                match self._topology.satellites.get(neighbor_id):
                    case Satellite() as neighbor_node:
                        neighbor_name = neighbor_node.name
                    case None:
                        match self._topology.ground_stations.get(neighbor_id):
                            case GroundStation() as neighbor_node:  # type: ignore[misc]
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
                iface = self._topology._get_interface_to_peer(sat, gs.id)
                if iface is None or iface.peer is None:
                    continue
                gs_loopback = gs.loopback.ipv4 if gs.loopback else None
                if not gs_loopback:
                    continue
                # The label-stack pass owns this prefix when it has an SR route;
                # keep the plain route only as a fallback when it does not.
                if ("ip", f"{gs_loopback}/32") in stack_keys:
                    continue
                gs_ip = iface.peer.ipv4
                iface_name = iface.get_iname()
                route_commands.append(
                    f"ip route replace {gs_loopback}/32 via {gs_ip} dev {iface_name}"
                )

            prev = prev_node_sid_routes.get(sat.id, [])
            foreign_keys = cached_route_keys(prev_sat_routes.get(sat.id, []))
            stale, changed = route_changes(prev, route_commands, foreign_keys)

            if stale or changed:
                batch_cmd = build_batch(stale, changed)
                try:
                    sat.execute_command(["sh", "-lc", batch_cmd])
                except Exception as e:
                    logging.error(f"Failed to install SR forwarding on {sat.name}: {e}")
                    return False
            prev_node_sid_routes[sat.id] = route_commands
            return True

        errors = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(install_sr_forwarding, sat)  # type: ignore[arg-type]
                for sat in all_sats
                if sat.container
            ]
            for fut in as_completed(futures):
                try:
                    if not fut.result():
                        errors += 1
                except Exception as e:
                    errors += 1
                    logging.error(f"SR forwarding installation error: {e}")
        return errors

    def apply_sr_routes_to_ground_stations(
        self,
        label_manager: Any,
        sr_lfib: dict[int, list],
        prev_gs_routes: dict[int, list[str]],
    ) -> int:
        """Install SR-MPLS routes on all ground stations.

        On the first call (previous table empty) all routes are installed.
        On subsequent calls only routes whose attributes changed are sent,
        and routes no longer present are deleted.

        Args:
            label_manager: Manager providing Node SIDs.
            sr_lfib: Ingress entries keyed by source node ID.
            prev_gs_routes: This pass's cache, mutated in place.

        Returns:
            Number of ground stations whose routes could not be installed.
        """
        if label_manager is None:
            return 0

        if not self._topology.ground_stations:
            return 0

        workers = min(
            32,
            (os.cpu_count() or 4) * 4,
            len(self._topology.ground_stations),
        )

        def install_gs_routes(gs: GroundStation) -> bool:
            route_commands = []

            my_sid = label_manager.get_node_sid(gs.id)
            if my_sid is not None:
                own_entry = SRNodeSIDEntry(node_sid=my_sid, node_name=gs.name)
                route_commands.append(own_entry.to_iproute2_command())

            for entry in sr_lfib.get(gs.id, []):
                route_commands.append(entry.to_iproute2_command())

            prev = prev_gs_routes.get(gs.id, [])
            prev_set = set(prev)
            new_set = set(route_commands)
            changed = [line for line in route_commands if line not in prev_set]
            stale = [line for line in prev if line not in new_set]

            if changed or stale:
                batch_cmd = build_batch(stale, changed)
                try:
                    gs.execute_command(["sh", "-lc", batch_cmd])
                    prev_gs_routes[gs.id] = route_commands
                except Exception as e:
                    logging.error(f"Failed to install SR routes on {gs.name}: {e}")
                    return False
            return True

        errors = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(install_gs_routes, gs)  # type: ignore[arg-type]
                for gs in self._topology.ground_stations.values()
                if gs.container
            ]
            for fut in as_completed(futures):
                try:
                    if not fut.result():
                        errors += 1
                except Exception as e:
                    errors += 1
                    logging.error(f"GS SR route installation error: {e}")
        return errors

    def apply_sr_routes_to_satellites(
        self,
        label_manager: Any,
        sr_lfib: dict[int, list],
        prev_sat_routes: dict[int, list[str]],
        prev_node_sid_routes: dict[int, list[str]],
    ) -> int:
        """Install SR-MPLS label-stack routes on all satellites.

        This pass owns the IPv4 prefix routes built from ``sr_lfib`` (the
        ``encap mpls`` routes and their direct-IP fallbacks). The satellite's
        own Node SID and its per-neighbour forward rules belong to
        ``apply_sr_node_sids_to_satellites`` and are not installed here, so
        every route has exactly one owner. Deletes for prefixes the SID pass
        still caches are skipped; that pass deletes them when it drops them.

        On the first call (previous table empty) all routes are installed.
        On subsequent calls only routes whose attributes changed are sent,
        and routes no longer present are deleted.

        Args:
            label_manager: Manager providing Node SIDs.
            sr_lfib: Ingress entries keyed by source node ID.
            prev_sat_routes: This pass's cache, mutated in place.
            prev_node_sid_routes: The SID pass's cache, read for ownership.

        Returns:
            Number of satellites whose routes could not be installed.
        """
        if label_manager is None:
            return 0

        if not self._topology.satellites:
            return 0

        workers = min(
            32,
            (os.cpu_count() or 4) * 4,
            len(self._topology.satellites),
        )

        def install_satellite_routes(sat: Satellite) -> bool:
            route_commands = [
                entry.to_iproute2_command() for entry in sr_lfib.get(sat.id, [])
            ]

            prev = prev_sat_routes.get(sat.id, [])
            foreign_keys = cached_route_keys(prev_node_sid_routes.get(sat.id, []))
            stale, changed = route_changes(prev, route_commands, foreign_keys)

            if stale or changed:
                batch_cmd = build_batch(stale, changed)
                try:
                    sat.execute_command(["sh", "-lc", batch_cmd])
                except Exception as e:
                    logging.error(f"Failed to install SR routes on {sat.name}: {e}")
                    return False
            prev_sat_routes[sat.id] = route_commands
            return True

        errors = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(install_satellite_routes, sat)  # type: ignore[arg-type]
                for sat in [
                    satellite
                    for satellite in self._topology.satellites.values()
                    if satellite.is_addressable()
                ]
                if sat.container
            ]
            for fut in as_completed(futures):
                try:
                    if not fut.result():
                        errors += 1
                except Exception as e:
                    errors += 1
                    logging.error(f"Satellite SR route installation error: {e}")
        return errors

    def install_source_routes(
        self,
        source: GroundStation,
        label_manager: Any,
        entries: list,
        prev_gs_routes: dict[int, list[str]],
    ) -> None:
        """Install all desired ingress routes for one source.

        Called after a custom-path change so only the affected source is
        reconfigured. Every desired command is always re-sent; stale route
        identities are deleted in the same fail-fast batch.

        Args:
            source: The ground station whose routes are installed.
            label_manager: Manager providing the source's Node SID.
            entries: The freshly planned ingress entries.
            prev_gs_routes: The per-source cache, mutated in place on success.
        """
        route_commands = []
        my_sid = label_manager.get_node_sid(source.id)
        if my_sid is not None:
            own_entry = SRNodeSIDEntry(node_sid=my_sid, node_name=source.name)
            route_commands.append(own_entry.to_iproute2_command())

        for entry in entries:
            route_commands.append(entry.to_iproute2_command())

        stale, _ = route_changes(
            prev_gs_routes.get(source.id, []), route_commands, set()
        )
        batch_cmd = build_batch(stale, route_commands)

        if batch_cmd:
            try:
                source.execute_command(["sh", "-lc", batch_cmd])
                prev_gs_routes[source.id] = route_commands
                logging.info(f"Updated {len(entries)} SR routes on {source.name}")
            except Exception as e:
                logging.error(f"Failed to update SR routes on {source.name}: {e}")
