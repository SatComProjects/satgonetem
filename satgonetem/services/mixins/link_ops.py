"""LinkOpsMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from satgonetem.utils.constants import MAX_WORKERS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from typing import List


class LinkOpsMixin:
    """LinkOps functionality."""

    def bulk_link_operations(
        self,
        to_add: bool = True,
        to_update: bool = True,
        to_del: bool = True,
        max_workers: int = MAX_WORKERS,
    ) -> dict:
        """
        Perform all link operations (add, update, delete) using a single gRPC channel
        with parallelized operations to optimize performance.

        Args:
            to_add: Whether to process link additions
            to_update: Whether to process link updates
            to_del: Whether to process link deletions
            max_workers: Maximum number of parallel workers

        Returns:
            dict: Contains counts and timing information for link operations:
                - 'added_count': Number of links added
                - 'updated_count': Number of links updated
                - 'deleted_count': Number of links deleted
                - 'add_time_total': Total time to add all links
                - 'add_time_per_link': Time per link for add operations
                - 'update_time_total': Total time to update all links
                - 'update_time_per_link': Time per link for update operations
                - 'delete_time_total': Total time to delete all links
                - 'delete_time_per_link': Time per link for delete operations
        """
        # Collect all link operations that need to be performed
        links_to_add = [
            link for link in self.links.values() if getattr(link, "to_add", False)
        ]
        links_to_update = [
            link for link in self.links.values() if getattr(link, "to_update", False)
        ]
        links_to_remove = [
            link for link in self.links.values() if getattr(link, "to_remove", False)
        ]

        # Initialize timing information
        operation_stats = {
            "added_count": len(links_to_add),
            "updated_count": len(links_to_update),
            "deleted_count": len(links_to_remove),
            "links_to_add": links_to_add,
            "add_time_total": 0.0,
            "add_time_per_link": 0.0,
            "update_time_total": 0.0,
            "update_time_per_link": 0.0,
            "delete_time_total": 0.0,
            "delete_time_per_link": 0.0,
        }

        # If no gRPC operations needed or status is False, return early
        if not self.get_status() or not (
            links_to_add or links_to_update or links_to_remove
        ):
            return operation_stats

        # Direct veth/tc operations (no gRPC)
        t_delete_start = time.perf_counter()
        t_update_start = time.perf_counter()
        t_add_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=1) as executor:
            futures = []

            if to_del:
                t_delete_start = time.perf_counter()
                for link in links_to_remove:
                    futures.append(executor.submit(self._execute_link_delete, link))

            if to_update:
                t_update_start = time.perf_counter()
                for link in links_to_update:
                    futures.append(executor.submit(self._execute_link_update, link))

            if to_add:
                t_add_start = time.perf_counter()
                for link in links_to_add:
                    futures.append(executor.submit(self._execute_link_add, link))

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"Link operation failed: {e}")

        t_end = time.perf_counter()
        operation_stats["delete_time_total"] = t_end - t_delete_start
        operation_stats["update_time_total"] = t_end - t_update_start
        operation_stats["add_time_total"] = t_end - t_add_start

        # Calculate per-link timing
        if operation_stats["added_count"] > 0:
            operation_stats["add_time_per_link"] = (
                operation_stats["add_time_total"] / operation_stats["added_count"]
            )
        if operation_stats["updated_count"] > 0:
            operation_stats["update_time_per_link"] = (
                operation_stats["update_time_total"] / operation_stats["updated_count"]
            )
        if operation_stats["deleted_count"] > 0:
            operation_stats["delete_time_per_link"] = (
                operation_stats["delete_time_total"] / operation_stats["deleted_count"]
            )

        return operation_stats

    def _update_routing_after_link_changes(
        self, new_links: List[Link], max_workers: int = 4
    ) -> None:
        """
        Update routing after link topology changes based on the configured routing method.

        Args:
            new_links: List of newly added links (used for FRR interface initialization)
            max_workers: Maximum number of worker threads for parallel processing
        """
        # Only update routing if routing has been explicitly initialized
        if not self.get_routing_initiated():
            logging.debug("Skipping routing update - routing not initialized")
            return
        if not self.routing_daemon:
            logging.debug("Skipping routing update - routing daemon not initialized")
            return

        _builtin_methods = {
            "dynamic-ospf",
            "dynamic-isis",
            "sr-mpls",
            "static",
        }

        if self.routing in _builtin_methods or self.routing in self._daemon_registry:
            self.routing_daemon.update(new_links, max_workers=max_workers)
            logging.info(f"Updated {self.routing} routing after link changes")
        else:
            logging.warning(
                f"Unknown routing method '{self.routing}', skipping routing update"
            )

    def _execute_link_delete(self, link: Link) -> None:
        """Delete a link's veth pair directly via nsenter+ip."""
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.teardown_link(link)
            link.to_delete = False
            return
        if self.launcher is None:
            logging.warning("launcher not set, skipping link delete")
            return
        try:
            self.launcher.delete_link(link)
            link.to_delete = False
            logging.info(f"Deleted link {link.source.name} -- {link.target.name}")
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to delete link {link.source.name}--{link.target.name}: {err}"
            )

    def _execute_link_update(self, link) -> None:
        """Update a link's netem delay and TBF rate directly via nsenter+tc."""
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.update_link(link)
            return
        if self.launcher is None:
            logging.warning("launcher not set, skipping link update")
            return
        try:
            self.launcher.update_link(link)
            delay = max(int(link.delay), 1)
            logging.info(
                f"Updated link {link.source.name} -- {link.target.name} with delay {delay} ms"
            )
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to update link {link.source.name}--{link.target.name}: {err}"
            )

    def _execute_link_add(self, link) -> None:
        """Add a link by creating a veth pair and applying qdiscs directly."""
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.setup_link(link)
            return
        if self.launcher is None:
            logging.warning("launcher not set, skipping link add")
            return
        try:
            self.launcher.add_link(link)
            delay = max(int(link.delay), 1)
            logging.info(
                f"Added link {link.source.name} -- {link.target.name} with delay {delay} ms"
            )
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to add link {link.source.name}--{link.target.name}: {err}"
            )

    def _process_links_sequentially(
        self, links_to_remove, links_to_update, links_to_add
    ) -> None:
        """Fallback method for sequential link processing."""
        for link in links_to_remove:
            self._execute_link_delete(link)
        for link in links_to_update:
            self._execute_link_update(link)
        for link in links_to_add:
            self._execute_link_add(link)

    def _perform_local_link_operations(
        self,
        to_add: bool = True,
        to_update: bool = True,
        to_del: bool = True,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        """
        Perform local link operations (interface management, state updates)
        without gRPC calls, using parallel processing where safe.

        Args:
            to_add: Whether to process link additions.
            to_update: Whether to process link updates.
            to_del: Whether to process link deletions.
            max_workers: Maximum number of parallel workers.
        """
        links_to_remove = (
            [link for link in self.links.values() if getattr(link, "to_remove", False)]
            if to_del
            else []
        )
        links_to_update = (
            [link for link in self.links.values() if getattr(link, "to_update", False)]
            if to_update
            else []
        )
        links_to_add = (
            [link for link in self.links.values() if getattr(link, "to_add", False)]
            if to_add
            else []
        )

        total_operations = (
            len(links_to_remove) + len(links_to_update) + len(links_to_add)
        )

        if total_operations == 0:
            return

        self._perform_local_link_operations_parallel(
            links_to_remove, links_to_update, links_to_add, max_workers
        )

    def _perform_local_link_operations_parallel(
        self,
        links_to_remove: list[Link],
        links_to_update: list[Link],
        links_to_add: list[Link],
        max_workers: int,
    ) -> None:
        """
        Perform local link operations in parallel where thread-safe.
        """
        # Thread-safe operations that can be parallelized
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []

            # Parallel operations for individual link processing
            # Remove interface operations (can be done in parallel per link)
            for link in links_to_remove:
                future = executor.submit(self._remove_link_interfaces, link)
                futures.append(future)

            # Update operations (thread-safe flag updates)
            for link in links_to_update:
                future = executor.submit(self._update_link_flags, link)
                futures.append(future)

            # Add interface operations (can be done in parallel per link)
            for link in links_to_add:
                future = executor.submit(self._add_link_interfaces, link)
                futures.append(future)

            # Wait for all parallel operations to complete
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"Local link operation failed: {e}")

        # Sequential operations that require thread safety (modifying shared collections)
        self._cleanup_removed_links_sequential(links_to_remove)

    def _remove_link_interfaces(self, link: Link) -> None:
        """Remove interfaces from nodes for a single link."""
        try:
            # Remove interfaces from nodes
            source: Node = link.source
            target: Node = link.target
            source.remove_interface_connected_to_node(target)
            target.remove_interface_connected_to_node(source)
        except (AttributeError, KeyError, TypeError) as e:
            logging.error(
                f"Failed to remove interfaces for link {link.source.name}--{link.target.name}: {e}"
            )

    def _cleanup_removed_links_sequential(self, links_to_remove: list[Link]) -> None:
        """
        Clean up removed links from shared data structures.
        Must be done sequentially to ensure thread safety.
        """
        for link in links_to_remove:
            try:
                # Remove interfaces from global list
                self.interfaces = [
                    intf for intf in self.interfaces if intf not in link.peer_interfaces
                ]
                # Remove link from dictionary
                key = self._build_link_key(link.source, link.target)
                del self.links[key]
            except (AttributeError, KeyError, TypeError) as e:
                logging.error(
                    f"Failed to cleanup link {link.source.name}--{link.target.name}: {e}"
                )

    def _update_link_flags(self, link: Link) -> None:
        """Update flags for a single link."""
        try:
            link.to_update = False
        except (AttributeError, TypeError) as e:
            logging.error(
                f"Failed to update flags for link {link.source.name}--{link.target.name}: {e}"
            )

    def _add_link_interfaces(self, link: Link) -> None:
        """Add interfaces for a single link."""
        try:
            self._build_interfaces_from_link(link, set_ip=True, sync_to_node=True)
            link.to_add = False
        except (AttributeError, KeyError, TypeError) as e:
            logging.error(
                f"Failed to add interfaces for link {link.source.name}--{link.target.name}: {e}"
            )
