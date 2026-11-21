"""Shared Bird lifecycle for OSPF and IS-IS daemons.

The daemons differ in their configuration builders and MPLS preparation, not
in how they start/stop/reload Bird across the topology.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, List, Optional

from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.routing.bird_service import stop_bird

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node


class BirdRoutingDaemon(RoutingDaemon):
    """Base daemon for Bird-backed routing protocols."""

    protocol_name = "Bird"

    def _validate_nodes(self, nodes: list["Node"]) -> bool:
        """Return False when the protocol cannot run on the selected image."""
        return True

    def init(self, max_workers: int = 4) -> bool:
        nodes = list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        )
        if not self._validate_nodes(nodes):
            return False

        errors = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._init_node, node): node for node in nodes}
            for future in as_completed(futures):
                node = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors += 1
                    logging.error(
                        "%s init failed on %s: %s",
                        self.protocol_name,
                        node.name,
                        exc,
                    )
        return errors == 0

    def update(self, new_links: "List[Link]", max_workers: int = 4) -> None:
        if not self.topology.status:
            logging.warning(
                "%s update skipped: topology not yet active", self.protocol_name
            )
            return

        affected = {link.source for link in new_links} | {
            link.target for link in new_links
        }
        for node in affected:
            try:
                self._init_node(node)
            except Exception as exc:
                logging.error(
                    "%s update failed on %s: %s",
                    self.protocol_name,
                    node.name,
                    exc,
                )

    def remove(self, node: "Optional[Node]" = None, max_workers: int = 4) -> None:
        if node is not None:
            self._stop_node(node)
            return

        nodes = list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        )
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._stop_node, n): n for n in nodes}
            for future in as_completed(futures):
                node = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logging.error(
                        "Failed to stop Bird on %s: %s", node.name, exc
                    )

    def _init_node(self, node: "Node") -> None:
        raise NotImplementedError

    def _stop_node(self, node: "Node") -> None:
        stop_bird(node)
