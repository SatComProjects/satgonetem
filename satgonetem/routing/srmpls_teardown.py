"""SR-MPLS teardown: flush kernel routes and drop per-node caches.

Extracted from ``SRMPLSDaemon``. The teardown owns the per-node cancel
sequence: recover the cached IPv4 prefixes, pop the node's cache entries,
delete those prefixes, then flush the kernel MPLS table. Full and scoped state
resets stay in the daemon because they own the daemon's lifecycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def cached_prefixes(commands: Iterable[str]) -> list[str]:
    """Return unique IPv4 prefixes installed by cached command lines.

    Only lines starting exactly with ``ip route replace `` carry a prefix this
    daemon installed. ``ip -f mpls route replace`` lines are local label rules
    and have no IPv4 prefix to delete first; they are flushed with the kernel
    MPLS table. Encapsulated lines still start with ``ip route replace``.

    Args:
        commands: Cached route command strings, possibly multi-line.

    Returns:
        Prefixes in first-seen order, without duplicates.
    """
    prefixes: list[str] = []
    for command in commands:
        for line in command.splitlines():
            line = line.strip()
            if not line.startswith("ip route replace "):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            prefix = parts[3]
            if prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


class SRMPLSTeardown:
    """Flush SR-MPLS state from one node or every node in a topology."""

    def __init__(self, caches: Iterable[dict[int, list[str]]]) -> None:
        """Bind the cache dictionaries by reference, never by copy.

        Args:
            caches: The daemon's per-pass route caches. The same dict objects
                must be mutated so the daemon observes the removals.
        """
        self._caches = tuple(caches)

    def flush_node(self, node: Any) -> None:
        """Flush SR-MPLS and IPv4 routes on a single node container.

        Removes the IPv4 prefixes installed by this daemon (including those
        using ``encap mpls``) and flushes the kernel MPLS forwarding table.
        Routes are recovered from the per-node caches before the node's cache
        entries are dropped.

        Args:
            node: The node whose routes should be flushed.
        """
        if node.container is None:
            return

        cached_commands: list[str] = []
        for cache in self._caches:
            cached_commands.extend(cache.get(node.id, []))

        succeeded = True
        for prefix in cached_prefixes(cached_commands):
            try:
                node.execute_command(["sh", "-lc", f"ip route del {prefix}"])
            except Exception as e:
                message = str(e).lower()
                if "no such process" in message or "cannot find" in message:
                    # Already gone on a retry after a partial flush.
                    continue
                succeeded = False
                logging.error(f"Failed to delete SR route {prefix} on {node.name}: {e}")

        try:
            node.execute_command(["sh", "-lc", "ip -f mpls route flush table all"])
        except Exception as e:
            succeeded = False
            logging.error(f"Failed to flush MPLS routes on {node.name}: {e}")

        if succeeded:
            for cache in self._caches:
                cache.pop(node.id, None)

    def flush_all_nodes(
        self,
        nodes: Iterable[Any],
        max_workers: int,
        flush_node: Callable[[Any], None],
    ) -> None:
        """Flush MPLS forwarding tables on all nodes in parallel.

        Args:
            nodes: Satellites and ground stations to flush.
            max_workers: Maximum number of parallel worker threads.
            flush_node: Per-node flush callable, resolved by the caller so a
                daemon-level patch still intercepts every node.
        """
        node_list = list(nodes)
        if not node_list:
            return

        workers = min(max_workers, len(node_list))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(flush_node, node): node
                for node in node_list
                if node.container
            }
            for fut in as_completed(futures):
                node = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"MPLS flush error on {node.name}: {e}")
