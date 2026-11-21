"""Pure static-route planning and diffing helpers.

Extracted from ``StaticRoutingDaemon`` so route diffing can be tested without
containers or a topology manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


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
    live_devs: Optional[set[str]] = None,
) -> list[RouteOperation]:
    """Diff previous and current routing tables into minimal operations."""
    if prev_tbl is curr_tbl:
        return []

    current = {entry_key(entry): entry for entry in curr_tbl}
    previous = {entry_key(entry): entry for entry in prev_tbl}
    current_prefixes = {
        (entry.destination, entry.get_prefix()) for entry in curr_tbl
    }
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
    live_devs: Optional[set[str]] = None,
) -> list[str]:
    """Return legacy ``ip route`` command strings for a table diff."""
    return [
        format_route_operation(op)
        for op in emit_route_operations(prev_tbl, curr_tbl, live_devs)
    ]
