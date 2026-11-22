"""Deterministic IPv6 address planning for emulated satellite topologies.

This planner mirrors :class:`satonetem.addressing.ipv4.Ipv4AddressPlan` without
touching it.  It is deliberately free of Docker, gRPC, and model dependencies.
It takes node IDs, node kinds, and opaque link keys, and returns immutable
address objects.

Layout of every node block
--------------------------

Each node receives one contiguous block (``/64`` by default)::

    <block base + 0>        network address, never assigned
    <block base + 1>/128    loopback, the stable routing target
    <block base + 2..3>     reserved
    <block base + 4>/127    first point-to-point link
    <block base + 6>/127    second point-to-point link
    ...

Link ownership follows the IPv4 planner: ground stations own their links, then
user terminals, then the endpoint with the lower node ID.  The owner always
receives the lower address of the ``/127``, making the pair independent of the
incidental ``Link.source``/``Link.target`` orientation.
"""

from __future__ import annotations

import heapq
import ipaddress
import threading
from collections.abc import Hashable
from dataclasses import dataclass, field

from satgonetem.addressing.ipv4 import (
    AddressPlanConfigurationError,
    AddressPlanError,
    AddressPlanExhausted,
    NodeKind,
)

__all__ = [
    "Ipv6AddressPlan",
    "Ipv6LinkAddress",
    "Ipv6NodeAddress",
]

# Offset inside a node block reserved for the loopback.
_LOOPBACK_OFFSET = 1
# First address offset used by a /127 point-to-point link.
_FIRST_LINK_OFFSET = 4
# Every link consumes two addresses.
_LINK_SIZE = 2
# Point-to-point link prefix length.
_LINK_PREFIX_LEN = 127
# Loopback prefix length.
_LOOPBACK_PREFIX_LEN = 128


@dataclass(frozen=True)
class Ipv6NodeAddress:
    """IPv6 addresses owned by one node."""

    node_id: int
    kind: NodeKind
    slot: int
    block: ipaddress.IPv6Network
    loopback: ipaddress.IPv6Interface

    @property
    def loopback_ip(self) -> str:
        return str(self.loopback.ip)


@dataclass(frozen=True)
class Ipv6LinkAddress:
    """The two IPv6 addresses and the blocking prefix of one link."""

    owner_id: int
    slot: int
    prefix_len: int
    source: ipaddress.IPv6Interface
    target: ipaddress.IPv6Interface


@dataclass
class _OwnerSlots:
    """Per-owner /127 slot allocator with lowest-free-slot reuse."""

    capacity: int
    next_slot: int = 0
    released: list[int] = field(default_factory=list)
    used: set[int] = field(default_factory=set)

    def allocate(self, owner_id: int, block: ipaddress.IPv6Network) -> int:
        while self.released:
            slot = heapq.heappop(self.released)
            if slot not in self.used:
                self.used.add(slot)
                return slot

        if self.next_slot >= self.capacity:
            raise AddressPlanExhausted(
                f"Node {owner_id} has no free /{_LINK_PREFIX_LEN} slots left in "
                f"{block} ({self.capacity} link slots per block)"
            )

        slot = self.next_slot
        self.next_slot += 1
        self.used.add(slot)
        return slot

    def release(self, slot: int) -> None:
        if slot in self.used:
            self.used.remove(slot)
            heapq.heappush(self.released, slot)


def _as_ipv6_network(
    value: str | ipaddress.IPv6Network, label: str
) -> ipaddress.IPv6Network:
    if isinstance(value, ipaddress.IPv4Network):
        raise AddressPlanConfigurationError(
            f"{label} must be an IPv6 network; got {value!r}"
        )
    try:
        network = (
            value
            if isinstance(value, ipaddress.IPv6Network)
            else ipaddress.ip_network(str(value), strict=True)
        )
    except ValueError as exc:
        raise AddressPlanConfigurationError(
            f"{label} must be an IPv6 network in CIDR notation with no host "
            f"bits set; got {value!r}"
        ) from exc

    if not isinstance(network, ipaddress.IPv6Network):
        raise AddressPlanConfigurationError(
            f"{label} must be an IPv6 network; got {value!r}"
        )
    return network


def _validate_pool(network: ipaddress.IPv6Network, label: str) -> None:
    """Reject ranges that are plainly unsuitable for a data-plane plan."""
    if network.is_multicast:
        raise AddressPlanConfigurationError(f"{label} cannot be multicast")
    if network.is_loopback:
        raise AddressPlanConfigurationError(f"{label} cannot be loopback space")
    if network.is_link_local:
        raise AddressPlanConfigurationError(f"{label} cannot be link-local space")
    if network.network_address.is_unspecified:
        raise AddressPlanConfigurationError(
            f"{label} cannot start at the unspecified address"
        )
    if network.is_global:
        raise AddressPlanConfigurationError(
            f"{label} must be a non-global IPv6 range; {network} is globally routable"
        )
    if not network.is_private:
        raise AddressPlanConfigurationError(
            f"{label} must be a private or unique-local IPv6 range; "
            f"{network} is not private"
        )


def _blocks_in_pool(pool: ipaddress.IPv6Network, block_prefix_len: int) -> int:
    if block_prefix_len <= pool.prefixlen:
        return 0
    return 1 << (block_prefix_len - pool.prefixlen)


def _block_size(block_prefix_len: int) -> int:
    return 1 << (128 - block_prefix_len)


class Ipv6AddressPlan:
    """Allocate /128 loopbacks and point-to-point /127s from ULA node blocks.

    Args:
        ground_station_pool: Pool subdivided into ground-station blocks.
        satellite_pool: Pool subdivided into satellite blocks.
        user_terminal_pool: Pool subdivided into user-terminal blocks.
        ground_station_prefix_len: Prefix length of each ground-station block.
        satellite_prefix_len: Prefix length of each satellite block.
        user_terminal_prefix_len: Prefix length of each user-terminal block.

    Raises:
        AddressPlanConfigurationError: If a pool/prefix combination is invalid
            or two pools overlap.
    """

    def __init__(
        self,
        *,
        ground_station_pool: str | ipaddress.IPv6Network = "fd00:1::/40",
        satellite_pool: str | ipaddress.IPv6Network = "fd00:2::/40",
        user_terminal_pool: str | ipaddress.IPv6Network = "fd00:3::/40",
        ground_station_prefix_len: int = 64,
        satellite_prefix_len: int = 64,
        user_terminal_prefix_len: int = 64,
    ) -> None:
        specs = (
            (
                NodeKind.GROUND_STATION,
                "ground_station_pool",
                ground_station_pool,
                ground_station_prefix_len,
            ),
            (
                NodeKind.SATELLITE,
                "satellite_pool",
                satellite_pool,
                satellite_prefix_len,
            ),
            (
                NodeKind.USER_TERMINAL,
                "user_terminal_pool",
                user_terminal_pool,
                user_terminal_prefix_len,
            ),
        )

        self._pools: dict[NodeKind, ipaddress.IPv6Network] = {}
        self._prefix_lens: dict[NodeKind, int] = {}
        self._capacities: dict[NodeKind, int] = {}

        for kind, label, pool_value, prefix_len in specs:
            if (
                isinstance(prefix_len, bool)
                or not isinstance(prefix_len, int)
                or not (0 <= prefix_len <= 128)
            ):
                raise AddressPlanConfigurationError(
                    f"{kind.value} prefix length must be an integer in [0, 128]"
                )
            pool = _as_ipv6_network(pool_value, label)
            _validate_pool(pool, label)
            if prefix_len <= pool.prefixlen:
                raise AddressPlanConfigurationError(
                    f"{kind.value} block prefix /{prefix_len} does not fit inside "
                    f"{label} {pool}; it must be longer than /{pool.prefixlen}"
                )
            block_size = _block_size(prefix_len)
            if block_size <= _FIRST_LINK_OFFSET + 1:
                raise AddressPlanConfigurationError(
                    f"{kind.value} block /{prefix_len} is too small to hold a "
                    "loopback and point-to-point links"
                )
            capacity = (block_size - _FIRST_LINK_OFFSET) // _LINK_SIZE
            self._pools[kind] = pool
            self._prefix_lens[kind] = prefix_len
            self._capacities[kind] = capacity

        kinds = list(self._pools)
        for index, left_kind in enumerate(kinds):
            for right_kind in kinds[index + 1 :]:
                if self._pools[left_kind].overlaps(self._pools[right_kind]):
                    raise AddressPlanConfigurationError(
                        f"{left_kind.value} pool {self._pools[left_kind]} overlaps "
                        f"{right_kind.value} pool {self._pools[right_kind]}"
                    )

        self._lock = threading.RLock()
        self._nodes: dict[int, Ipv6NodeAddress] = {}
        self._next_node_slot: dict[NodeKind, int] = {kind: 0 for kind in self._pools}
        self._owner_slots: dict[int, _OwnerSlots] = {}
        self._links: dict[Hashable, Ipv6LinkAddress] = {}

    # ------------------------------------------------------------------
    # Node blocks
    # ------------------------------------------------------------------

    def register_node(self, node_id: int, kind: NodeKind) -> Ipv6NodeAddress:
        """Register a node and return its block and loopback.

        Node slots are assigned in registration order per kind.  Callers
        should register nodes in a deterministic order (for example sorted by
        node ID) so the plan is reproducible.
        """
        if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id < 0:
            raise AddressPlanError(
                f"Node ID must be a non-negative integer, got {node_id!r}"
            )
        if not isinstance(kind, NodeKind):
            raise AddressPlanError(f"kind must be a NodeKind, got {kind!r}")

        with self._lock:
            existing = self._nodes.get(node_id)
            if existing is not None:
                if existing.kind is not kind:
                    raise AddressPlanError(
                        f"Node {node_id} is already registered as "
                        f"{existing.kind.value}, cannot re-register as {kind.value}"
                    )
                return existing

            slot = self._next_node_slot[kind]
            pool = self._pools[kind]
            prefix_len = self._prefix_lens[kind]
            block_size = _block_size(prefix_len)
            block_start = int(pool.network_address) + slot * block_size
            block_end = block_start + block_size - 1
            pool_end = int(pool.network_address) + pool.num_addresses - 1
            if block_start > pool_end or block_end > pool_end:
                capacity = _blocks_in_pool(pool, prefix_len)
                raise AddressPlanExhausted(
                    f"{kind.value} pool {pool} is full: capacity is {capacity} "
                    f"/{prefix_len} blocks"
                )

            block = ipaddress.IPv6Network((block_start, prefix_len))
            loopback = ipaddress.IPv6Interface(
                f"{ipaddress.IPv6Address(block_start + _LOOPBACK_OFFSET)}"
                f"/{_LOOPBACK_PREFIX_LEN}"
            )
            address = Ipv6NodeAddress(
                node_id=node_id,
                kind=kind,
                slot=slot,
                block=block,
                loopback=loopback,
            )
            self._nodes[node_id] = address
            self._next_node_slot[kind] = slot + 1
            self._owner_slots[node_id] = _OwnerSlots(capacity=self._capacities[kind])
            return address

    def node_address(self, node_id: int) -> Ipv6NodeAddress:
        """Return a registered node's address object."""
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise AddressPlanError(
                f"Node {node_id} is not registered in the plan"
            ) from exc

    def node_addresses(self) -> tuple[Ipv6NodeAddress, ...]:
        """Return all registered node addresses in registration order."""
        return tuple(self._nodes.values())

    # ------------------------------------------------------------------
    # Point-to-point links
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_key(address: Ipv6NodeAddress) -> tuple[int, int]:
        return (address.kind.priority, address.node_id)

    def allocate_link(
        self,
        link_key: Hashable,
        source_id: int,
        target_id: int,
    ) -> Ipv6LinkAddress:
        """Allocate (or return) the /127 address pair for a link.

        The owner is selected by :attr:`NodeKind.priority` and then by the
        lower node ID.  The lower-priority endpoint always receives the lower
        address of the /127, making the result independent of link orientation.
        """
        with self._lock:
            existing = self._links.get(link_key)
            if existing is not None:
                return existing

            source = self.node_address(source_id)
            target = self.node_address(target_id)
            if source.node_id == target.node_id:
                raise AddressPlanError(
                    f"Link {link_key!r} cannot connect node {source.node_id} to itself"
                )

            owner = min(source, target, key=self._endpoint_key)
            lower = min(source, target, key=self._endpoint_key)
            owner_slots = self._owner_slots[owner.node_id]
            slot = owner_slots.allocate(owner.node_id, owner.block)

            low_int = (
                int(owner.block.network_address)
                + _FIRST_LINK_OFFSET
                + _LINK_SIZE * slot
            )
            high_int = low_int + 1
            block_end = int(owner.block.network_address) + (
                owner.block.num_addresses - 1
            )
            if high_int > block_end:
                owner_slots.release(slot)
                raise AddressPlanExhausted(
                    f"Link slot {slot} for node {owner.node_id} does not fit in "
                    f"{owner.block}"
                )

            low = ipaddress.IPv6Address(low_int)
            high = ipaddress.IPv6Address(high_int)
            source_ip, target_ip = (
                (low, high) if lower.node_id == source.node_id else (high, low)
            )
            address = Ipv6LinkAddress(
                owner_id=owner.node_id,
                slot=slot,
                prefix_len=_LINK_PREFIX_LEN,
                source=ipaddress.IPv6Interface(f"{source_ip}/{_LINK_PREFIX_LEN}"),
                target=ipaddress.IPv6Interface(f"{target_ip}/{_LINK_PREFIX_LEN}"),
            )
            self._links[link_key] = address
            return address

    def release_link(self, link_key: Hashable) -> None:
        """Release a link's /127 slot, if it was allocated."""
        with self._lock:
            address = self._links.pop(link_key, None)
            if address is None:
                return
            owner_slots = self._owner_slots.get(address.owner_id)
            if owner_slots is not None:
                owner_slots.release(address.slot)

    def link_address(self, link_key: Hashable) -> Ipv6LinkAddress | None:
        """Return an already-allocated link address, or ``None``."""
        return self._links.get(link_key)

    def link_addresses(self) -> tuple[Ipv6LinkAddress, ...]:
        """Return all allocated link addresses in allocation order."""
        return tuple(self._links.values())

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def pool_for(self, kind: NodeKind) -> ipaddress.IPv6Network:
        """Return the configured pool for *kind*."""
        return self._pools[kind]

    def prefix_len_for(self, kind: NodeKind) -> int:
        """Return the configured block prefix length for *kind*."""
        return self._prefix_lens[kind]

    def capacity_for(self, kind: NodeKind) -> int:
        """Return the number of node blocks available for *kind*."""
        return _blocks_in_pool(self._pools[kind], self._prefix_lens[kind])

    def link_capacity_for(self, node_id: int) -> int:
        """Return the number of /127 slots in a node's block."""
        return self._owner_slots[node_id].capacity
