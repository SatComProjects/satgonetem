"""Pure static-route planning and diffing helpers.

Extracted from ``StaticRoutingDaemon`` so route diffing can be tested without
containers or a topology manager.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteOperation:
    """One typed route operation sent through the RouteInstall RPC."""

    action: str
    destination: str
    gateway: str
    device: str


def entry_key(entry) -> tuple:
    """Return an immutable key for a RoutingEntry used in diffs."""
    return (
        entry.destination,
        entry.get_prefix(),
        entry.gateway,
        entry.interface.get_iname(),
    )


def route_operation(action: str, entry) -> RouteOperation:
    """Build one typed route operation from a RoutingEntry."""
    return RouteOperation(
        action=action,
        destination=entry.destination + entry.get_prefix(),
        gateway=entry.gateway or "",
        device=entry.interface.get_iname(),
    )


def format_route_operation(operation: RouteOperation) -> str:
    """Format a typed route operation for the legacy ``ip -batch`` path."""
    if operation.gateway:
        return (
            f"route {operation.action} {operation.destination} "
            f"via {operation.gateway} dev {operation.device}"
        )
    return f"route {operation.action} {operation.destination} dev {operation.device}"


def emit_route_operations(
    prev_tbl: Iterable,
    curr_tbl: Iterable,
    live_devs: set[str] | None = None,
) -> list[RouteOperation]:
    """Diff previous and current routing tables into minimal operations."""
    if prev_tbl is curr_tbl:
        return []

    current = {entry_key(entry): entry for entry in curr_tbl}
    previous = {entry_key(entry): entry for entry in prev_tbl}
    current_prefixes = {(entry.destination, entry.get_prefix()) for entry in curr_tbl}
    removed = [
        route_operation("del", entry)
        for key, entry in previous.items()
        if key not in current
        and (entry.destination, entry.get_prefix()) not in current_prefixes
        and (live_devs is None or entry.interface.get_iname() in live_devs)
    ]
    added = [
        route_operation("replace", entry)
        for key, entry in current.items()
        if key not in previous
    ]
    return removed + added


def emit_batch_commands(
    prev_tbl: Iterable,
    curr_tbl: Iterable,
    live_devs: set[str] | None = None,
) -> list[str]:
    """Return legacy ``ip route`` command strings for a table diff."""
    return [
        format_route_operation(op)
        for op in emit_route_operations(prev_tbl, curr_tbl, live_devs)
    ]


def route_cache_signature(
    src_id: int,
    src_ifaces,
    dst_array,
    next_hops: dict[int, dict[int, int]],
    loopback_ips: dict[int, str],
) -> list[tuple[int, str, str, int | None]]:
    """Build the route-cache signature for one source.

    A next-hop vector alone does not identify the destinations it targets:
    replacing a destination with another node can leave every next hop
    unchanged.  Each entry therefore carries the destination identity and
    the gateway as well, so any change to the installed routes invalidates
    the cache.

    Args:
        src_id: Integer ID of the source node.
        src_ifaces: Mapping of peer node ID to outgoing Interface.
        dst_array: Ordered list of destination nodes.
        next_hops: Mapping of destination ID to ``{src_id: next_hop}``.
        loopback_ips: Mapping of node ID to its exact loopback IP.

    Returns:
        One ``(dst_id, destination, gateway, next_hop)`` tuple per
        destination, in ``dst_array`` order.

    Raises:
        ValueError: If a reachable destination's next hop has no entry in
            *src_ifaces* or that interface has no peer. Silently falling
            back to an on-link route would install a route the topology
            cannot use.
    """
    signature: list[tuple[int, str, str, int | None]] = []
    for dst in dst_array:
        next_hop = next_hops[dst.id].get(src_id)
        gateway = ""
        if next_hop is not None:
            iface_map = src_ifaces or {}
            interface = iface_map.get(int(next_hop))
            if interface is None:
                raise ValueError(
                    f"Source {src_id} has no interface to next hop {next_hop} "
                    f"for destination {dst.id}"
                )
            if interface.peer is None:
                raise ValueError(
                    f"Source {src_id} interface {interface.name!r} to next hop "
                    f"{next_hop} for destination {dst.id} has no peer"
                )
            gateway = interface.peer.ipv4
        signature.append((dst.id, loopback_ips[dst.id], gateway, next_hop))
    return signature
