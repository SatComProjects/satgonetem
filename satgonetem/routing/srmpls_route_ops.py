"""Pure SR-MPLS route identity, diffing, and batch helpers.

Extracted from ``SRMPLSDaemon`` so route identity and batch assembly can be
tested without containers or a topology manager. Nothing in this module holds
state or talks to a node.
"""

from __future__ import annotations


def route_key(route_command: str) -> tuple[str, str]:
    """Return the stable identity of an installed route command.

    A route is identified by its table and label/prefix, not by the full
    command: two commands that target the same route are the same route even
    when their next hop or label stack differs.

    Args:
        route_command: Installed route command ending in ``replace``.

    Returns:
        ``("mpls", label)`` for MPLS routes, ``("ip", prefix)`` for IPv4 routes.

    Raises:
        ValueError: If *route_command* is not a supported replace command.
    """
    parts = route_command.split()
    if (
        parts[:4] == ["ip", "-f", "mpls", "route"]
        and len(parts) >= 6
        and parts[4] == "replace"
    ):
        return ("mpls", parts[5])
    if parts[:3] == ["ip", "route", "replace"] and len(parts) >= 5:
        return ("ip", parts[3])
    raise ValueError(f"Unsupported route command: {route_command!r}")


def delete_command(route_command: str) -> str:
    """Build the iproute2 delete command for an installed route line.

    Cached route lines always use the ``replace`` form emitted by
    :meth:`SRNodeSIDEntry.to_iproute2_command`,
    :meth:`SRForwardEntry.to_iproute2_command`, and
    :meth:`SRLabelStackEntry.to_iproute2_command`. The delete form only
    needs the MPLS label or IP prefix; next-hop attributes are omitted.

    Args:
        route_command: Installed route command ending in ``replace``.

    Returns:
        Matching ``ip ... route del`` command.

    Raises:
        ValueError: If *route_command* is not a supported replace command.
    """
    family, target = route_key(route_command)
    if family == "mpls":
        return f"ip -f mpls route del {target}"
    return f"ip route del {target}"


def tolerant_delete_command(route_command: str) -> str:
    """Build a delete that is tolerant only of an already-absent route.

    A cached route can outlive the kernel entry (container restart, or a
    partially applied earlier batch), so deleting it directly under ``set -e``
    would abort the whole batch and leave the stale key in the cache forever.

    The delete is therefore guarded by an explicit ``ip ... route show``
    existence check: an absent route is skipped, while a permission or kernel
    failure on a route that does exist still aborts the batch. iproute2 uses
    exit code 2 for an absent route, but it also uses it for kernel errors, so
    the exit code alone cannot distinguish absence from a real failure.

    Args:
        route_command: Installed route command ending in ``replace``.

    Returns:
        Matching conditional ``ip ... route del`` command.
    """
    family, target = route_key(route_command)
    delete = delete_command(route_command)
    if family == "mpls":
        show = f"ip -f mpls route show exact {target}"
    else:
        show = f"ip route show exact {target}"
    return f"if {show} 2>&1 | grep -q .; then {delete}; fi"


def route_changes(
    previous: list[str],
    desired: list[str],
    foreign_keys: set[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Diff cached satellite routes against the routes this pass now wants.

    Routes are compared by identity (label/prefix), so a changed next hop is a
    replace rather than a delete followed by a fresh install.

    Args:
        previous: Commands this pass installed on the previous update.
        desired: Commands this pass wants installed now.
        foreign_keys: Route identities owned by the sibling satellite pass.
            They are never deleted here: the owner deletes them when it drops
            the route, which prevents one pass from breaking the other's batch.

    Returns:
        Tuple of ``(delete_lines, install_lines)``.
    """
    previous_by_key = {route_key(line): line for line in previous}
    desired_by_key = {route_key(line): line for line in desired}
    deletes = [
        line
        for key, line in previous_by_key.items()
        if key not in desired_by_key and key not in foreign_keys
    ]
    installs = [
        line for key, line in desired_by_key.items() if previous_by_key.get(key) != line
    ]
    return deletes, installs


def cached_route_keys(commands: list[str]) -> set[tuple[str, str]]:
    """Return the route identities present in a cached command list."""
    return {route_key(line) for line in commands}


def build_batch(deletes: list[str], installs: list[str]) -> str:
    """Assemble one fail-fast ``sh -lc`` batch for route deletes and installs.

    Deletes are made tolerant of an already-absent kernel route; a delete that
    fails while the route exists aborts the batch, so the stale cache entry is
    kept and retried. Installs stay fatal for the same reason.

    Args:
        deletes: Stale route commands to remove.
        installs: Desired route commands to install.

    Returns:
        The batch command, or ``""`` when there is nothing to do.
    """
    lines = [tolerant_delete_command(line) for line in deletes] + list(installs)
    if not lines:
        return ""
    return "set -e; " + "; ".join(lines)
