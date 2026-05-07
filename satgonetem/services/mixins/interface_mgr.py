"""InterfaceMgrMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from satgonetem.utils.constants import MAX_WORKERS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.node import Node
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link
    from typing import Optional


class InterfaceMgrMixin:
    """InterfaceMgr functionality."""

    def get_all_links_usage(self):
        """Return usage for all links (mapped), not only congested.

        Returns:
            List of dictionaries with link usage data:
            - src: Source node name
            - dst: Destination node name
            - value: Bandwidth usage in bits per second (from interface monitoring)
            - type: Link type ('InterSatelliteLink' or 'GroundStationLink')
        """
        if not self.get_gonetem_status():
            return []
        return self.monitor_links()

    def _add_loopback_interfaces_to_list(self):
        """
        Method to add loopback interfaces to the list of interfaces
        """
        for node in list(self.satellites.values()) + list(
            self.ground_stations.values()
        ):
            node.loopback.name = f"lo_{node.name}"
            node.loopback.set_ipv4_address()
            self.interfaces.append(node.loopback)

    def _assign_interfaces_to_nodes(self) -> None:
        """
        Method to assign interfaces to nodes
        """
        links = self.links.values()

        for link in links:
            self._build_interfaces_from_link(link)

    def _build_interfaces_from_link(
        self, link: Link, set_ip: bool = True, sync_to_node: bool = True
    ) -> None:
        # Get source and destination
        source = link.source
        target = link.target

        # print(source, target, type)

        int1 = source.create_interface(f"{source.name}.{str(target.id)}")
        int2 = target.create_interface(f"{target.name}.{str(source.id)}")

        # Set delays
        int1.delay = link.delay
        int2.delay = link.delay

        # Set type
        if "Gnd" in source.name[:3] and "Gnd" in target.name[:3]:
            int1.type = "GroundObjectLink"
            int2.type = "GroundObjectLink"
        elif "Gnd" in [source.name[:3], target.name[:3]]:
            int1.type = "GroundStationLink"
            int2.type = "GroundStationLink"

        elif "Sat" in [source.name[:3], target.name[:3]]:
            int1.type = "InterSatelliteLink"
            int2.type = "InterSatelliteLink"
        else:
            logging.info(f"Unknown link type for {source.name} -> {target.name}")

        # Set peer
        int1.peer = int2
        int2.peer = int1

        # Set state
        int1.is_active = link.is_active
        int2.is_active = link.is_active

        link.peer_interfaces.extend([int1, int2])  # Add interfaces to the link

        self.interfaces.extend([int1, int2])  # Add interfaces to the global list

        if not self.get_status():
            return

        if set_ip:
            int1.set_ipv4_address()
            int2.set_ipv4_address()

        if sync_to_node and set_ip:
            source.set_ipv4_to_containers(interface=int1, set_lo=False)
            target.set_ipv4_to_containers(interface=int2, set_lo=False)

    def _set_ips_to_nodes(self) -> None:
        """
        Method to set IPs to nodes
        """
        for interface in self.interfaces:
            interface.set_ipv4_address()

    def set_ipv4s_for_all_nodes(
        self, set_lo: bool = True, max_workers: int = MAX_WORKERS, sats: bool = True
    ) -> None:
        """
        Assign IPv4s to every satellite and ground station as fast as possible with a spinner.
        """
        nodes = []
        if sats:
            nodes.extend(self.get_satellites())
        nodes.extend(self.get_ground_stations())

        if not nodes:
            logging.info(
                "No nodes to configure (satellites + ground stations list is empty)."
            )
            return

        total_nodes = len(nodes)
        tic = time.perf_counter()

        if max_workers is None:
            max_workers = min(MAX_WORKERS, total_nodes)

        submitted = 0
        errors = 0

        # Start the spinner with a distinct color for network config

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    node.set_ipv4_to_containers, interface=None, set_lo=set_lo
                ): node
                for node in nodes
            }

            for fut in as_completed(futures):
                node = futures[fut]
                submitted += 1
                try:
                    fut.result()
                except Exception as e:
                    errors += 1
                    logging.exception(
                        f"Failed to dispatch IPv4 assignment on {node.name}: {e}"
                    )

                logging.debug(
                    f"IP Assignment: {submitted}/{total_nodes} (Errors: {errors})"
                )

        toc = time.perf_counter()

        logging.info(
            f"IPv4 assignment dispatched for {submitted - errors}/{submitted} nodes "
            f"in {toc - tic:.3f}s (max_workers={max_workers})."
        )

    def _get_interface_to_peer(self, node: Node, peer_id: int) -> Optional[Interface]:
        """
        Get the interface on a node that connects to a specific peer.

        Args:
            node: Source node
            peer_id: ID of the peer node

        Returns:
            Interface connecting to peer, or None if not found
        """
        for iface in node.interfaces:
            # Interface name format: NodeName.PeerID
            try:
                iface_peer_id = int(iface.name.split(".")[1])
                if iface_peer_id == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None
