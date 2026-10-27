"""LinkOpsMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from satgonetem.utils.constants import MAX_WORKERS, ROUTE_APPLY_WORKERS

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from typing import List


def _collect_link_operation_lists(
    links,
    links_to_add: "List[Link] | None" = None,
    links_to_update: "List[Link] | None" = None,
    links_to_remove: "List[Link] | None" = None,
) -> tuple[list[Link], list[Link], list[Link]]:
    """Return add/update/remove lists, scanning ``links`` at most once."""
    if (
        links_to_add is not None
        and links_to_update is not None
        and links_to_remove is not None
    ):
        return links_to_add, links_to_update, links_to_remove

    scanned_add: list[Link] = []
    scanned_update: list[Link] = []
    scanned_remove: list[Link] = []
    for link in links.values():
        if getattr(link, "to_add", False):
            scanned_add.append(link)
        if getattr(link, "to_update", False):
            scanned_update.append(link)
        if getattr(link, "to_remove", False):
            scanned_remove.append(link)

    return (
        links_to_add if links_to_add is not None else scanned_add,
        links_to_update if links_to_update is not None else scanned_update,
        links_to_remove if links_to_remove is not None else scanned_remove,
    )


class LinkOpsMixin:
    """LinkOps functionality."""

    def bulk_link_operations(
        self,
        to_add: bool = True,
        to_update: bool = True,
        to_del: bool = True,
        max_workers: int = MAX_WORKERS,
        links_to_add: "List[Link] | None" = None,
        links_to_update: "List[Link] | None" = None,
        links_to_remove: "List[Link] | None" = None,
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
                - 'add_time_total': Sum of actual add-operation durations
                - 'add_time_wall': First add submission to last add completion
                - 'add_time_per_link': Average actual add-operation duration
                - 'update_time_total': Sum of actual update-operation durations
                - 'update_time_wall': First update submission to last completion
                - 'update_time_per_link': Average actual update-operation duration
                - 'delete_time_total': Sum of actual delete durations
                - 'delete_time_wall': First delete submission to last completion
                - 'delete_time_per_link': Average actual delete-operation duration
        """
        # Collect all link operations that need to be performed. Callers that
        # already have the lists pass them in so this scan is done only once
        # per simulation step.
        links_to_add, links_to_update, links_to_remove = (
            _collect_link_operation_lists(
                self.links,
                links_to_add,
                links_to_update,
                links_to_remove,
            )
        )

        processed_add = links_to_add if to_add else []
        processed_update = links_to_update if to_update else []
        processed_remove = links_to_remove if to_del else []

        # Initialize timing information.  *_time_total is the sum of the actual
        # per-operation durations; *_time_wall is the first-submit-to-last-
        # completion span for that category.
        operation_stats = {
            "added_count": len(processed_add),
            "updated_count": len(processed_update),
            "deleted_count": len(processed_remove),
            "links_to_add": processed_add,
            "add_time_total": 0.0,
            "add_time_wall": 0.0,
            "add_time_per_link": 0.0,
            "update_time_total": 0.0,
            "update_time_wall": 0.0,
            "update_time_per_link": 0.0,
            "delete_time_total": 0.0,
            "delete_time_wall": 0.0,
            "delete_time_per_link": 0.0,
        }

        # If no gRPC operations needed or status is False, return early
        if not self.get_status() or not (
            processed_add or processed_update or processed_remove
        ):
            return operation_stats

        durations: dict[str, list[float]] = {
            "add": [],
            "update": [],
            "delete": [],
        }
        started: dict[str, float] = {}
        finished: dict[str, float] = {}
        submit_times: dict = {}
        future_categories: dict = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []

            def submit(category: str, operation, link) -> None:
                submitted_at = time.perf_counter()
                future = executor.submit(operation, link)
                submit_times[future] = submitted_at
                future_categories[future] = category
                started.setdefault(category, submitted_at)
                futures.append(future)

            if to_del:
                for link in processed_remove:
                    submit("delete", self._execute_link_delete, link)

            if to_update:
                for link in processed_update:
                    submit("update", self._execute_link_update, link)

            if to_add:
                for link in processed_add:
                    submit("add", self._execute_link_add, link)

            for future in as_completed(futures):
                category = future_categories[future]
                try:
                    duration = future.result()
                except Exception as e:
                    logging.error(f"Link operation failed: {e}")
                    duration = time.perf_counter() - submit_times[future]
                else:
                    try:
                        duration = float(duration)
                    except (TypeError, ValueError):
                        # Mocks and non-timed callers do not return a duration.
                        duration = time.perf_counter() - submit_times[future]

                durations[category].append(duration)
                finished[category] = time.perf_counter()

        for category, prefix in (
            ("add", "add"),
            ("update", "update"),
            ("delete", "delete"),
        ):
            category_durations = durations[category]
            total = sum(category_durations)
            operation_stats[f"{prefix}_time_total"] = total
            operation_stats[f"{prefix}_time_per_link"] = (
                total / len(category_durations) if category_durations else 0.0
            )
            if category in started and category in finished:
                operation_stats[f"{prefix}_time_wall"] = (
                    finished[category] - started[category]
                )

        return operation_stats

    def _update_routing_after_link_changes(
        self, new_links: List[Link], max_workers: int = ROUTE_APPLY_WORKERS
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

    def _execute_link_delete(self, link: Link) -> float:
        """Delete a link's veth pair directly via nsenter+ip."""
        start = time.perf_counter()
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.teardown_link(link)
            link.to_delete = False
            return time.perf_counter() - start
        if self.launcher is None:
            logging.warning("launcher not set, skipping link delete")
            return 0.0
        try:
            self.launcher.delete_link(link)
            link.to_delete = False
            logging.debug(
                "Deleted link %s -- %s", link.source.name, link.target.name
            )
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to delete link {link.source.name}--{link.target.name}: {err}"
            )
        return time.perf_counter() - start

    def _execute_link_update(self, link) -> float:
        """Update a link's netem delay and TBF rate directly via nsenter+tc."""
        start = time.perf_counter()
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.update_link(link)
            return time.perf_counter() - start
        if self.launcher is None:
            logging.warning("launcher not set, skipping link update")
            return 0.0
        try:
            self.launcher.update_link(link)
            delay = max(int(link.delay), 1)
            logging.debug(
                "Updated link %s -- %s with delay %d ms",
                link.source.name,
                link.target.name,
                delay,
            )
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to update link {link.source.name}--{link.target.name}: {err}"
            )
        return time.perf_counter() - start

    def _execute_link_add(self, link) -> float:
        """Add a link by creating a veth pair and applying qdiscs directly."""
        start = time.perf_counter()
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.setup_link(link)
            return time.perf_counter() - start
        if self.launcher is None:
            logging.warning("launcher not set, skipping link add")
            return 0.0
        try:
            self.launcher.add_link(link)
            delay = max(int(link.delay), 1)
            logging.debug(
                "Added link %s -- %s with delay %d ms",
                link.source.name,
                link.target.name,
                delay,
            )
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to add link {link.source.name}--{link.target.name}: {err}"
            )
        return time.perf_counter() - start

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
        links_to_add: "List[Link] | None" = None,
        links_to_update: "List[Link] | None" = None,
        links_to_remove: "List[Link] | None" = None,
    ) -> None:
        """
        Perform local link operations (interface management, state updates)
        without gRPC calls, using parallel processing where safe.

        Args:
            to_add: Whether to process link additions.
            to_update: Whether to process link updates.
            to_del: Whether to process link deletions.
            max_workers: Maximum number of parallel workers.
            links_to_add: Optional precomputed additions.
            links_to_update: Optional precomputed updates.
            links_to_remove: Optional precomputed removals.
        """
        links_to_add, links_to_update, links_to_remove = (
            _collect_link_operation_lists(
                self.links,
                links_to_add,
                links_to_update,
                links_to_remove,
            )
        )
        if not to_del:
            links_to_remove = []
        if not to_update:
            links_to_update = []
        if not to_add:
            links_to_add = []

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
        # Interface removals mutate shared node.interfaces lists. Run them
        # sequentially; concurrent scans/removals can skip entries.
        for link in links_to_remove:
            self._remove_link_interfaces(link)

        # Thread-safe operations that can be parallelized
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []

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
            source = link.source
            target = link.target
            source.remove_interface_connected_to_node(target)
            target.remove_interface_connected_to_node(source)
            self.interface_version = getattr(self, "interface_version", 0) + 1
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
