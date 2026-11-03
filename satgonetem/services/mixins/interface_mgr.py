"""InterfaceMgrMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from satgonetem.addressing import Ipv4AddressPlan, NodeKind
from satgonetem.utils.constants import MAX_WORKERS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.node import Node
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link
    from typing import Optional


class InterfaceMgrMixin:
    """Build interfaces and apply the topology-wide IPv4 address plan."""

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

    def _initialize_address_plan(self) -> None:
        """Build the topology-wide IPv4 plan and assign node/loopback addresses.

        Nodes are registered in sorted-ID order per kind, so the plan is
        reproducible for a given topology.  Links are pre-allocated in a
        canonical order so initial addresses do not depend on dictionary
        insertion order.
        """
        plan = Ipv4AddressPlan(
            ground_station_pool=self.ground_station_pool,
            satellite_pool=self.satellite_pool,
            user_terminal_pool=self.user_terminal_pool,
            ground_station_prefix_len=self.ground_station_prefix_len,
            satellite_prefix_len=self.satellite_prefix_len,
            user_terminal_prefix_len=self.user_terminal_prefix_len,
        )

        def register(node: Node, kind: NodeKind) -> None:
            address = plan.register_node(node.id, kind)
            node.address_block = address.block
            node.loopback.set_ipv4(str(address.loopback.ip), 32)
            satcom_object = getattr(node, "satcom_object", None)
            if satcom_object is not None and hasattr(
                satcom_object, "address_block"
            ):
                satcom_object.address_block = str(address.block)

        for satellite in sorted(self.satellites.values(), key=lambda n: n.id):
            register(satellite, NodeKind.SATELLITE)
        for ground_object in sorted(
            self.ground_stations.values(), key=lambda n: n.id
        ):
            kind = (
                NodeKind.USER_TERMINAL
                if getattr(ground_object, "type", "") == "UserTerminal"
                else NodeKind.GROUND_STATION
            )
            register(ground_object, kind)

        for link in sorted(self.links.values(), key=self._address_link_sort_key):
            plan.allocate_link(
                self._link_key(link), link.source.id, link.target.id
            )

        self.address_plan = plan

    @staticmethod
    def _link_key(link: Link) -> frozenset[str]:
        """Return the key used by ``self.links`` for this link."""
        return frozenset((link.source.name, link.target.name))

    @staticmethod
    def _address_link_sort_key(link: Link) -> tuple[int, int]:
        """Canonical order for initial link-slot allocation."""
        return tuple(sorted((link.source.id, link.target.id)))

    def _add_loopback_interfaces_to_list(self):
        """Add loopback interfaces to the global interface list."""
        for node in list(self.satellites.values()) + list(
            self.ground_stations.values()
        ):
            node.loopback.name = f"lo_{node.name}"
            if not node.loopback.ipv4:
                raise RuntimeError(
                    f"Loopback address for {node.name} was not assigned by the "
                    "IPv4 address plan"
                )
            self.interfaces.append(node.loopback)

    def _assign_interfaces_to_nodes(self) -> None:
        """Create and configure interfaces for every link."""
        for link in self.links.values():
            self._build_interfaces_from_link(link)

    def _build_interfaces_from_link(
        self, link: Link, set_ip: bool = True, sync_to_node: bool = True
    ) -> None:
        """Create the two interfaces for a link and apply their planned /31."""
        if link.peer_interfaces:
            # Idempotent: a link that already has interfaces must not create a
            # second pair with the same kernel names.
            return

        source = link.source
        target = link.target

        int1 = source.create_interface(f"{source.name}.{str(target.id)}")
        int2 = target.create_interface(f"{target.name}.{str(source.id)}")

        # Set delays
        int1.delay = link.delay
        int2.delay = link.delay

        # Set type directly from the modelled link so UserTerminalLink,
        # GroundStationLink, GroundObjectLink, and InterSatelliteLink all work.
        int1.type = link.type
        int2.type = link.type

        # Set peer
        int1.peer = int2
        int2.peer = int1

        # Set state
        int1.is_active = link.is_active
        int2.is_active = link.is_active

        link.peer_interfaces.extend([int1, int2])
        self.interfaces.extend([int1, int2])
        self.interface_version = getattr(self, "interface_version", 0) + 1

        if not set_ip:
            return

        if self.address_plan is None:
            raise RuntimeError(
                "IPv4 address plan is not initialized; call "
                "_initialize_address_plan() before creating link interfaces"
            )

        address = self.address_plan.allocate_link(
            self._link_key(link), source.id, target.id
        )
        int1.set_ipv4(str(address.source.ip), address.prefix_len)
        int2.set_ipv4(str(address.target.ip), address.prefix_len)

        if sync_to_node and self.get_status():
            source.set_ipv4_to_containers(interface=int1, set_lo=False)
            target.set_ipv4_to_containers(interface=int2, set_lo=False)

    def set_ipv4s_for_all_nodes(
        self,
        set_lo: bool = True,
        max_workers: int = MAX_WORKERS,
        satellites: bool = True,
        ground_stations: bool = True,
    ) -> None:
        """Apply every node's planned IPv4 addresses inside its container.

        One batched ``ip`` command is issued per node and nodes are processed
        in parallel.  The method raises only after all nodes have been
        attempted so a single transport failure does not hide the others.
        """
        nodes = []
        if satellites:
            nodes.extend(self.get_satellites())
        if ground_stations:
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
        failures: list[tuple[str, Exception]] = []

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
                except Exception as exc:  # noqa: BLE001 - aggregate per node
                    failures.append((node.name, exc))
                    logging.exception(
                        "Failed to dispatch IPv4 assignment on %s: %s",
                        node.name,
                        exc,
                    )

                logging.debug(
                    "IP Assignment: %s/%s (Errors: %s)",
                    submitted,
                    total_nodes,
                    len(failures),
                )

        toc = time.perf_counter()

        logging.info(
            "IPv4 assignment dispatched for %s/%s nodes in %.3fs "
            "(max_workers=%s, errors=%s).",
            submitted - len(failures),
            submitted,
            toc - tic,
            max_workers,
            len(failures),
        )

        if failures:
            names = ", ".join(name for name, _ in failures)
            raise RuntimeError(
                f"IPv4 assignment failed on {len(failures)} node(s): {names}"
            )

    def _get_interface_to_peer(self, node: Node, peer_id: int) -> Optional[Interface]:
        """Return the interface on *node* connected to *peer_id*, if any."""
        for iface in node.interfaces:
            try:
                iface_peer_id = int(iface.name.split(".")[1])
                if iface_peer_id == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None
