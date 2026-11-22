"""LinkOpsMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

import grpc

from satgonetem.proto import netem_pb2
from satgonetem.routing.registry import is_known_method
from satgonetem.services.link_backend import LinkBackend
from satgonetem.utils.constants import MAX_WORKERS, ROUTE_APPLY_WORKERS

if TYPE_CHECKING:
    from satgonetem.models.link import Link


def _collect_link_operation_lists(
    links,
    links_to_add: list[Link] | None = None,
    links_to_update: list[Link] | None = None,
    links_to_remove: list[Link] | None = None,
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

    interfaces: list

    def _link_backend(self) -> LinkBackend | None:
        """Return the injected backend, falling back to the launcher."""
        backend = getattr(self, "link_backend", None)
        return backend if backend is not None else getattr(self, "launcher", None)

    def _link_add_failures(self) -> set[frozenset[str]]:
        """Return link keys whose last backend add failed and remain pending.

        Entries are dropped when a retry succeeds or the link is deleted so a
        failed add never leaks local interfaces or retains stale state.
        """
        failures = getattr(self, "_failed_link_adds", None)
        if failures is None:
            failures = set()
            self._failed_link_adds = failures
        return failures

    def _link_update_failures(self) -> set[frozenset[str]]:
        """Return link keys whose last backend update failed.

        ``to_update`` stays set for these links so the next sync retries them;
        successful retries drop the entry. ``_update_link_flags`` also consults
        this set, because the local phase runs after the backend batch.
        """
        failures = getattr(self, "_failed_link_updates", None)
        if failures is None:
            failures = set()
            self._failed_link_updates = failures
        return failures

    def _link_backend_adds(self) -> set[frozenset[str]]:
        """Return link keys whose add already succeeded on the backend.

        A link can be present in the emulator while its local interfaces or
        addresses are still being retried. Those retries must not send a second
        ``LinkAdd``: the server rejects a duplicate and the local build would be
        blocked by the failure marker forever. Entries are dropped when the
        backend confirms deletion or the link is cleaned up.
        """
        applied = getattr(self, "_backend_link_adds", None)
        if applied is None:
            applied = set()
            self._backend_link_adds = applied
        return applied

    def bulk_link_operations(
        self,
        to_add: bool = True,
        to_update: bool = True,
        to_del: bool = True,
        max_workers: int = MAX_WORKERS,
        links_to_add: list[Link] | None = None,
        links_to_update: list[Link] | None = None,
        links_to_remove: list[Link] | None = None,
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
                - 'update_time_total': Batch wall time for LinkUpdateMany
                - 'update_time_wall': Same batch wall time
                - 'update_time_per_link': Batch wall time divided by updated_count
                - 'update_error_count': Per-link failures reported by the batch
                - 'delete_time_total': Sum of actual delete durations
                - 'delete_time_wall': First delete submission to last completion
                - 'delete_time_per_link': Average actual delete-operation duration
        """
        # Collect all link operations that need to be performed. Callers that
        # already have the lists pass them in so this scan is done only once
        # per simulation step.
        links_to_add, links_to_update, links_to_remove = _collect_link_operation_lists(
            self.links,
            links_to_add,
            links_to_update,
            links_to_remove,
        )

        processed_add = links_to_add if to_add else []
        processed_update = links_to_update if to_update else []
        processed_remove = links_to_remove if to_del else []

        # Initialize timing information. Add/delete use the sum of actual
        # per-operation durations; their *_time_wall is the first-submit-to-
        # last-completion span. Updates are one batch call, so all three update
        # figures derive from that call's wall time.
        operation_stats: dict[str, Any] = {
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
            "update_error_count": 0,
            "delete_time_total": 0.0,
            "delete_time_wall": 0.0,
            "delete_time_per_link": 0.0,
        }

        # If no gRPC operations needed or status is False, return early
        if not self.get_status() or not (
            processed_add or processed_update or processed_remove
        ):
            return operation_stats

        # HIL links keep the per-link path (they are driven by the HIL
        # manager, not the emulator). Everything else goes through the batch
        # RPC so the server takes one topology lock for the whole burst.
        hil_updates = []
        batch_updates = []
        hil_manager = getattr(self, "hil_manager", None)
        for link in processed_update:
            if hil_manager is not None and hil_manager.is_hil_link(link):
                hil_updates.append(link)
            else:
                batch_updates.append(link)

        update_batch_wall = 0.0
        if batch_updates:
            batch_start = time.perf_counter()
            try:
                _, batch_errors = LinkOpsMixin._apply_link_update_batch(
                    self, batch_updates
                )
            finally:
                update_batch_wall = time.perf_counter() - batch_start
            operation_stats["update_error_count"] = batch_errors

        durations: dict[str, list[float]] = {
            "add": [],
            "update": [],
            "delete": [],
        }
        started: dict[str, float] = {}
        finished: dict[str, float] = {}
        submit_times: dict = {}
        future_categories: dict = {}

        # Only create a pool when work remains after the batch call. A pure
        # update burst must not wrap LinkUpdateMany in a thread pool.
        if processed_remove or hil_updates or processed_add:
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
                    for link in hil_updates:
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
                            # Mocks and non-timed callers do not return a
                            # duration.
                            duration = time.perf_counter() - submit_times[future]

                    durations[category].append(duration)
                    finished[category] = time.perf_counter()

        # Clear update flags once the batch and any HIL updates are done.
        # _perform_local_link_operations clears them again as a safety net.
        # Failed links keep the flag so the next sync retries them.
        update_failures = LinkOpsMixin._link_update_failures(self)
        for link in processed_update:
            try:
                key = self._build_link_key(link.source, link.target)
                if key in update_failures:
                    continue
                link.to_update = False
            except (AttributeError, TypeError):
                logging.error(
                    "Failed to clear update flag for link %s--%s",
                    getattr(getattr(link, "source", None), "name", "?"),
                    getattr(getattr(link, "target", None), "name", "?"),
                )

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

        if hil_updates:
            update_failures = LinkOpsMixin._link_update_failures(self)
            hil_errors = 0
            for link in hil_updates:
                if self._build_link_key(link.source, link.target) in update_failures:
                    hil_errors += 1
            operation_stats["update_error_count"] = (
                int(operation_stats["update_error_count"]) + hil_errors
            )

        if batch_updates:
            # One batch call is the unit of work: wall, total and per-link
            # figures all derive from the same elapsed time.
            operation_stats["update_time_total"] = update_batch_wall
            operation_stats["update_time_wall"] = update_batch_wall
            operation_stats["update_time_per_link"] = (
                update_batch_wall / len(processed_update) if processed_update else 0.0
            )

        return operation_stats

    def _apply_link_update_batch(self, links: list[Link]) -> tuple[int, int]:
        """Update non-HIL links through ``LinkUpdateMany``.

        Falls back to the per-link ``_execute_link_update`` path when the
        launcher has no batch method or the server reports UNIMPLEMENTED. Other
        gRPC failures propagate so a burst is never silently dropped.

        Returns:
            ``(updated, errors)`` counted from the per-link results.
        """
        launcher = LinkOpsMixin._link_backend(self)
        if launcher is None:
            logging.warning("launcher not set, skipping link update batch")
            return 0, 0

        failures = LinkOpsMixin._link_update_failures(self)
        update_many = getattr(launcher, "update_links", None)
        if update_many is None or getattr(self, "_link_update_many_unavailable", False):
            for link in links:
                self._execute_link_update(link)
            error_count = len(
                [
                    link
                    for link in links
                    if self._build_link_key(link.source, link.target) in failures
                ]
            )
            return len(links), error_count

        try:
            response = update_many(links)
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
            self._link_update_many_unavailable = True
            logging.warning(
                "LinkUpdateMany is not implemented by the server; "
                "falling back to per-link LinkUpdate"
            )
            for link in links:
                self._execute_link_update(link)
            error_count = len(
                [
                    link
                    for link in links
                    if self._build_link_key(link.source, link.target) in failures
                ]
            )
            return len(links), error_count

        response_status = getattr(response, "status", None)
        if response_status is not None and response_status.code != netem_pb2.OK:
            raise RuntimeError(
                "LinkUpdateMany failed: "
                + (response_status.error or "server returned a non-OK status")
            )

        results = list(getattr(response, "results", ()))
        if len(results) != len(links):
            raise RuntimeError(
                "LinkUpdateMany returned "
                f"{len(results)} result(s) for {len(links)} link(s)"
            )

        updated = 0
        errors = 0
        for link, result in zip(links, results, strict=False):
            status = getattr(result, "status", None)
            key = self._build_link_key(link.source, link.target)
            if status is not None and status.code == netem_pb2.OK:
                updated += 1
                failures.discard(key)
                continue

            failures.add(key)
            errors += 1
            source = getattr(getattr(link, "source", None), "name", "?")
            target = getattr(getattr(link, "target", None), "name", "?")
            peer1 = getattr(result, "peer1", "") or source
            peer2 = getattr(result, "peer2", "") or target
            detail = getattr(status, "error", "") if status is not None else ""
            logging.error(
                "LinkUpdateMany failed for %s<->%s (%s<->%s): %s",
                source,
                target,
                peer1,
                peer2,
                detail or "unknown error",
            )

        return updated, errors

    def _update_routing_after_link_changes(
        self, new_links: list[Link], max_workers: int = ROUTE_APPLY_WORKERS
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

        if is_known_method(self.routing, getattr(self, "_daemon_registry", None)):
            self.routing_daemon.update(new_links, max_workers=max_workers)
            logging.info(f"Updated {self.routing} routing after link changes")
        else:
            logging.warning(
                f"Unknown routing method '{self.routing}', skipping routing update"
            )

    def _execute_link_delete(self, link: Link) -> float:
        """Delete a link's veth pair directly via nsenter+ip.

        ``link.to_delete`` stays set until the backend confirms the deletion.
        A failed attempt keeps the flag so local interfaces and manager state
        are retained for the next sync instead of being forgotten while the
        emulator still holds the veth pair.
        """
        start = time.perf_counter()
        key = self._build_link_key(link.source, link.target)
        applied = LinkOpsMixin._link_backend_adds(self)
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            link.to_delete = True
            if self.hil_manager.teardown_link(link):
                link.to_delete = False
                applied.discard(key)
            return time.perf_counter() - start
        launcher = LinkOpsMixin._link_backend(self)
        if launcher is None:
            logging.warning("launcher not set, skipping link delete")
            return 0.0
        link.to_delete = True
        try:
            launcher.delete_link(link)
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to delete link {link.source.name}--{link.target.name}: {err}"
            )
            return time.perf_counter() - start
        link.to_delete = False
        applied.discard(key)
        logging.debug("Deleted link %s -- %s", link.source.name, link.target.name)
        return time.perf_counter() - start

    def _execute_link_update(self, link) -> float:
        """Update a link's netem delay and TBF rate directly via nsenter+tc."""
        start = time.perf_counter()
        failures = LinkOpsMixin._link_update_failures(self)
        build_key = getattr(self, "_build_link_key", None)
        if build_key is not None:
            key = build_key(link.source, link.target)
        else:
            key = frozenset((link.source.name, link.target.name))
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            try:
                update_ok = self.hil_manager.update_link(link)
            except Exception as err:
                failures.add(key)
                logging.error(
                    "Unable to update HIL link %s--%s: %s",
                    link.source.name,
                    link.target.name,
                    err,
                )
                return time.perf_counter() - start
            if update_ok:
                failures.discard(key)
            else:
                failures.add(key)
            return time.perf_counter() - start
        launcher = LinkOpsMixin._link_backend(self)
        if launcher is None:
            logging.warning("launcher not set, skipping link update")
            return 0.0
        try:
            launcher.update_link(link)
        except (RuntimeError, OSError) as err:
            failures.add(key)
            logging.error(
                f"Unable to update link {link.source.name}--{link.target.name}: {err}"
            )
        else:
            failures.discard(key)
            delay = max(int(link.delay), 1)
            logging.debug(
                "Updated link %s -- %s with delay %d ms",
                link.source.name,
                link.target.name,
                delay,
            )
        return time.perf_counter() - start

    def _execute_link_add(self, link) -> float:
        """Add a link by creating a veth pair and applying qdiscs directly.

        The link stays in ``_link_add_failures`` until the backend confirms
        the creation. ``_add_link_interfaces`` skips failed links so local
        interfaces are only built after the emulator accepted the add, and
        ``to_add`` remains set so the next sync retries.
        """
        start = time.perf_counter()
        failures = self._link_add_failures()
        key = self._build_link_key(link.source, link.target)
        applied = LinkOpsMixin._link_backend_adds(self)
        if key in applied:
            # The emulator already accepted this link; only the local build is
            # still pending. Re-sending LinkAdd is rejected as a duplicate.
            failures.discard(key)
            return time.perf_counter() - start
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            try:
                setup_ok = self.hil_manager.setup_link(link)
            except Exception:
                failures.add(key)
                raise
            if setup_ok:
                failures.discard(key)
                applied.add(key)
            else:
                failures.add(key)
            return time.perf_counter() - start
        launcher = LinkOpsMixin._link_backend(self)
        if launcher is None:
            logging.warning("launcher not set, skipping link add")
            return 0.0
        failures.add(key)
        try:
            launcher.add_link(link)
        except (RuntimeError, OSError) as err:
            logging.error(
                f"Unable to add link {link.source.name}--{link.target.name}: {err}"
            )
            return time.perf_counter() - start
        failures.discard(key)
        applied.add(key)
        delay = max(int(link.delay), 1)
        logging.debug(
            "Added link %s -- %s with delay %d ms",
            link.source.name,
            link.target.name,
            delay,
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
        links_to_add: list[Link] | None = None,
        links_to_update: list[Link] | None = None,
        links_to_remove: list[Link] | None = None,
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
        links_to_add, links_to_update, links_to_remove = _collect_link_operation_lists(
            self.links,
            links_to_add,
            links_to_update,
            links_to_remove,
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
        # A failed backend delete leaves ``to_delete`` set on the link. Keep
        # its interfaces and manager state intact so the next sync can retry
        # instead of forgetting a link the emulator still holds.
        confirmed_removals = [
            link for link in links_to_remove if not getattr(link, "to_delete", False)
        ]

        # Interface removals mutate shared node.interfaces lists. Run them
        # sequentially; concurrent scans/removals can skip entries.
        for link in confirmed_removals:
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
        self._cleanup_removed_links_sequential(confirmed_removals)

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
                # Return the link's slots to both address plans so later
                # dynamic links can reuse them.
                link_key = frozenset((link.source.name, link.target.name))
                address_plan = getattr(self, "address_plan", None)
                if address_plan is not None:
                    address_plan.release_link(link_key)
                address_plan_v6 = getattr(self, "address_plan_v6", None)
                if address_plan_v6 is not None:
                    address_plan_v6.release_link(link_key)
                # Remove link from dictionary, dropping any stale pending-add
                # marker so a failed add cannot retain the removed link.
                key = self._build_link_key(link.source, link.target)
                self._link_add_failures().discard(key)
                self._link_update_failures().discard(key)
                self._link_backend_adds().discard(key)
                del self.links[key]
            except (AttributeError, KeyError, TypeError) as e:
                logging.error(
                    f"Failed to cleanup link {link.source.name}--{link.target.name}: {e}"
                )

    def _update_link_flags(self, link: Link) -> None:
        """Clear ``to_update`` unless the last backend update failed."""
        try:
            key = self._build_link_key(link.source, link.target)
            if key in LinkOpsMixin._link_update_failures(self):
                return
            link.to_update = False
        except (AttributeError, TypeError) as e:
            logging.error(
                f"Failed to update flags for link {link.source.name}--{link.target.name}: {e}"
            )

    def _add_link_interfaces(self, link: Link) -> None:
        """Add interfaces for a single link.

        Skipped while the backend add is unconfirmed so a failed add can be
        retried without creating local interfaces for a link the emulator
        never accepted.
        """
        key = self._build_link_key(link.source, link.target)
        if key in self._link_add_failures():
            return
        try:
            self._build_interfaces_from_link(link, set_ip=True, sync_to_node=True)
            link.to_add = False
        except Exception as e:  # noqa: BLE001 - keep to_add set for a retry
            logging.error(
                f"Failed to add interfaces for link {link.source.name}--{link.target.name}: {e}"
            )
