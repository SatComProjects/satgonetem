"""InterfaceMgrMixin for TopologyManager."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from satgonetem.addressing import Ipv4AddressPlan, Ipv6AddressPlan, NodeKind
from satgonetem.utils.constants import MAX_WORKERS

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


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
        """Build the topology-wide plans and assign node/loopback addresses.

        Nodes are registered in sorted-ID order per kind, so the plan is
        reproducible for a given topology.  Links are pre-allocated in a
        canonical order so initial addresses do not depend on dictionary
        insertion order.

        Both address families are validated and fully pre-allocated before any
        node is mutated.  A failure while building either plan therefore
        leaves every node and the manager's plan references untouched.
        """
        plan = Ipv4AddressPlan(
            ground_station_pool=self.ground_station_pool,
            satellite_pool=self.satellite_pool,
            user_terminal_pool=self.user_terminal_pool,
            ground_station_prefix_len=self.ground_station_prefix_len,
            satellite_prefix_len=self.satellite_prefix_len,
            user_terminal_prefix_len=self.user_terminal_prefix_len,
        )
        plan_v6 = self._build_ipv6_plan()

        registrations: list[tuple[Node, NodeKind]] = []
        for satellite in sorted(self.satellites.values(), key=lambda n: n.id):
            registrations.append((satellite, NodeKind.SATELLITE))
        for ground_station in sorted(self.ground_stations.values(), key=lambda n: n.id):
            registrations.append((ground_station, NodeKind.GROUND_STATION))

        # User terminals are not instantiated in this version, but the plan
        # already supports them.  Register them if a future model provides
        # them on the manager.
        for user_terminal in sorted(
            getattr(self, "user_terminals", {}).values(), key=lambda n: n.id
        ):
            registrations.append((user_terminal, NodeKind.USER_TERMINAL))

        # Validate and allocate both families before mutating any node.
        for node, kind in registrations:
            plan.register_node(node.id, kind)
            if plan_v6 is not None:
                plan_v6.register_node(node.id, kind)

        for link in sorted(self.links.values(), key=self._address_link_sort_key):
            key = self._link_key(link)
            plan.allocate_link(key, link.source.id, link.target.id)
            if plan_v6 is not None:
                plan_v6.allocate_link(key, link.source.id, link.target.id)

        for node, _kind in registrations:
            address = plan.node_address(node.id)
            node.address_block = address.block
            node.loopback.set_ipv4(str(address.loopback.ip), 32)
            if plan_v6 is not None:
                address_v6 = plan_v6.node_address(node.id)
                node.address_block_v6 = address_v6.block
                node.loopback.set_ipv6(str(address_v6.loopback.ip), 128)

        self.address_plan = plan
        self.address_plan_v6 = plan_v6

    def _build_ipv6_plan(self) -> Ipv6AddressPlan | None:
        """Return the configured IPv6 plan, or ``None`` when IPv6 is disabled."""
        if not getattr(self, "ipv6_enabled", False):
            return None
        return Ipv6AddressPlan(
            ground_station_pool=self.ipv6_ground_station_pool,
            satellite_pool=self.ipv6_satellite_pool,
            user_terminal_pool=self.ipv6_user_terminal_pool,
            ground_station_prefix_len=self.ipv6_ground_station_prefix_len,
            satellite_prefix_len=self.ipv6_satellite_prefix_len,
            user_terminal_prefix_len=self.ipv6_user_terminal_prefix_len,
        )

    @staticmethod
    def _link_key(link: Link) -> frozenset[str]:
        """Return the key used by ``self.links`` for this link."""
        return frozenset((link.source.name, link.target.name))

    @staticmethod
    def _address_link_sort_key(link: Link) -> tuple[int, int]:
        """Canonical order for initial link-slot allocation."""
        return (
            min(link.source.id, link.target.id),
            max(link.source.id, link.target.id),
        )

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

    def _get_interface_lock(self) -> threading.Lock:
        """Return the per-instance lock guarding interface mutations.

        ``TopologyManager`` initializes ``_interface_lock`` eagerly in
        ``__init__``. Lightweight test harnesses that compose the mixin
        without that initializer get a per-instance lock lazily here.
        """
        lock = getattr(self, "_interface_lock", None)
        if lock is None:
            lock = self.__dict__.setdefault("_interface_lock", threading.Lock())
        return lock

    def _assign_interfaces_to_nodes(self) -> None:
        """Create and configure interfaces for every active link."""
        for link in self.links.values():
            if not link.is_active:
                continue
            self._build_interfaces_from_link(link)

    def _build_interfaces_from_link(
        self, link: Link, set_ip: bool = True, sync_to_node: bool = True
    ) -> None:
        """Create the two interfaces for a link and apply their planned /31.

        Both address families are allocated before any interface is created.
        If a later step fails, newly allocated slots and partially created
        interfaces are rolled back so a retry starts from a clean state.
        """
        lock = self._get_interface_lock()
        source = link.source
        target = link.target

        address = None
        address_v6 = None
        plan_v6 = None
        key = None
        new_v4 = False
        new_v6 = False
        created: list[tuple[Node, Interface]] = []
        version_incremented = False

        try:
            with lock:
                if link.peer_interfaces:
                    # Idempotent: a link that already has interfaces must not create a
                    # second pair with the same kernel names.
                    return

                if set_ip:
                    if self.address_plan is None:
                        raise RuntimeError(
                            "IPv4 address plan is not initialized; call "
                            "_initialize_address_plan() before creating link interfaces"
                        )

                    key = self._link_key(link)
                    was_allocated_v4 = self.address_plan.link_address(key) is not None
                    address = self.address_plan.allocate_link(key, source.id, target.id)
                    new_v4 = not was_allocated_v4

                    if getattr(self, "ipv6_enabled", False):
                        plan_v6 = getattr(self, "address_plan_v6", None)
                        if plan_v6 is None:
                            raise RuntimeError(
                                "IPv6 address plan is not initialized; call "
                                "_initialize_address_plan() before creating link interfaces"
                            )
                        was_allocated_v6 = plan_v6.link_address(key) is not None
                        address_v6 = plan_v6.allocate_link(key, source.id, target.id)
                        new_v6 = not was_allocated_v6

                int1 = source.create_interface(f"{source.name}.{str(target.id)}")
                created.append((source, int1))
                int2 = target.create_interface(f"{target.name}.{str(source.id)}")
                created.append((target, int2))

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
                    logging.info(
                        f"Unknown link type for {source.name} -> {target.name}"
                    )

                # Set peer
                int1.peer = int2
                int2.peer = int1

                # Set state
                int1.is_active = link.is_active
                int2.is_active = link.is_active

                link.peer_interfaces.extend([int1, int2])
                self.interfaces.extend([int1, int2])
                self.interface_version = getattr(self, "interface_version", 0) + 1
                version_incremented = True

                if address is not None:
                    int1.set_ipv4(str(address.source.ip), address.prefix_len)
                    int2.set_ipv4(str(address.target.ip), address.prefix_len)

                if address_v6 is not None:
                    int1.set_ipv6(str(address_v6.source.ip), address_v6.prefix_len)
                    int2.set_ipv6(str(address_v6.target.ip), address_v6.prefix_len)

            # Push the planned addresses while the rollback block still owns
            # both the interfaces and the newly allocated slots. A transport
            # failure here must look exactly like any other construction
            # failure, otherwise the next tick sees peer_interfaces and
            # clears to_add without ever retrying the push.
            if set_ip and sync_to_node and self.get_status():
                source.set_ipv4_to_containers(interface=int1, set_lo=False)
                target.set_ipv4_to_containers(interface=int2, set_lo=False)
                if address_v6 is not None:
                    source.set_ipv6_to_containers(interface=int1, set_lo=False)
                    target.set_ipv6_to_containers(interface=int2, set_lo=False)
        except Exception:
            with lock:
                for owner, created_interface in created:
                    if created_interface in owner.interfaces:
                        owner.interfaces.remove(created_interface)
                    if created_interface in link.peer_interfaces:
                        link.peer_interfaces.remove(created_interface)
                    if created_interface in self.interfaces:
                        self.interfaces.remove(created_interface)
                if version_incremented:
                    self.interface_version = max(
                        getattr(self, "interface_version", 1) - 1, 0
                    )
                if new_v4 and self.address_plan is not None and key is not None:
                    self.address_plan.release_link(key)
                if new_v6 and plan_v6 is not None and key is not None:
                    plan_v6.release_link(key)
            raise

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

    def set_ipv6s_for_all_nodes(
        self,
        set_lo: bool = True,
        max_workers: int = MAX_WORKERS,
        satellites: bool = True,
        ground_stations: bool = True,
    ) -> None:
        """Apply every node's planned IPv6 addresses inside its container.

        One batched ``ip -6`` command is issued per node and nodes are
        processed in parallel.  The method raises only after all nodes have
        been attempted so a single transport failure does not hide the others.
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
                    node.set_ipv6_to_containers, interface=None, set_lo=set_lo
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
                        "Failed to dispatch IPv6 assignment on %s: %s",
                        node.name,
                        exc,
                    )

                logging.debug(
                    "IPv6 Assignment: %s/%s (Errors: %s)",
                    submitted,
                    total_nodes,
                    len(failures),
                )

        toc = time.perf_counter()

        logging.info(
            "IPv6 assignment dispatched for %s/%s nodes in %.3fs "
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
                f"IPv6 assignment failed on {len(failures)} node(s): {names}"
            )

    def _get_interface_to_peer(self, node: Node, peer_id: int) -> Interface | None:
        """Return the interface on *node* connected to *peer_id*, if any."""
        for iface in node.interfaces:
            try:
                iface_peer_id = int(iface.name.split(".")[1])
                if iface_peer_id == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None
