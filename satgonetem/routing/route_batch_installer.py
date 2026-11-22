"""RouteInstall batching for the static routing daemon.

Chunks typed route operations, sends them through the gRPC RouteInstall RPC,
and falls back to the legacy per-node ``ip -batch`` path when the server does
not implement RouteInstall.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import grpc

from satgonetem.proto import netem_pb2
from satgonetem.routing.static_route_ops import RouteOperation

# RouteInstall request limits.  The server runs a bounded worker pool inside
# one request, so we keep chunks small enough for the default 4 MiB gRPC
# message limit and send them one at a time.
MAX_ROUTE_NODES_PER_REQUEST = 1024
MAX_ROUTE_REQUEST_BYTES = 2 * 1024 * 1024
SLOW_ROUTE_DURATION_MICROS = 50_000


@dataclass
class _BatchOutcome:
    """Per-node outcome of one route-installation batch path."""

    ok_nodes: set[str] = field(default_factory=set)
    error_nodes: set[str] = field(default_factory=set)
    applied: int = 0
    durations: list[int] = field(default_factory=list)

    def merge(self, other: _BatchOutcome) -> None:
        self.ok_nodes.update(other.ok_nodes)
        self.error_nodes.update(other.error_nodes)
        self.applied += other.applied
        self.durations.extend(other.durations)


class _RouteInstallUnsupported(Exception):
    """Raised internally when the server does not implement RouteInstall."""


def _percentile(values: list[int], percent: float) -> int:
    """Return a nearest-rank percentile from already-computed durations."""
    if not values:
        return 0
    ordered = sorted(values)
    index = int(round((percent / 100.0) * (len(ordered) - 1)))
    return ordered[min(max(index, 0), len(ordered) - 1)]


class RouteBatchInstaller:
    """Install route batches through RouteInstall with a legacy fallback."""

    def __init__(self, topology) -> None:
        """Initialize the installer and its sticky RouteInstall support flag."""
        self._topology = topology
        # True until the server replies UNIMPLEMENTED.  Set to False once so
        # an old server is not probed on every simulation step.
        self._route_install_supported = True

    @property
    def route_install_supported(self) -> bool:
        """Whether the server is still expected to implement RouteInstall."""
        return self._route_install_supported

    def resolve_route_batch_node(self, key):
        """Resolve a node name key to a node object when possible."""
        if not isinstance(key, str):
            return key

        for attr in ("ground_stations", "satellites"):
            container = getattr(self._topology, attr, None)
            if isinstance(container, dict):
                for node in container.values():
                    if node.name == key:
                        return node

        for getter_name in ("get_ground_stations", "get_satellites"):
            getter = getattr(self._topology, getter_name, None)
            if callable(getter):
                try:
                    for node in getter():
                        if node.name == key:
                            return node
                except TypeError:
                    continue
        return None

    def partition_route_batches(self, jobs):
        """Split node jobs into gRPC-client groups and legacy jobs.

        In normal operation every node shares one client and project id, which
        produces a single group and therefore a single RouteInstall request.
        """
        groups: dict[tuple[int, str], tuple[Any, str, list]] = {}
        legacy_jobs = []
        for node, ops in jobs:
            client = getattr(node, "grpc_client", None)
            if client is None:
                legacy_jobs.append((node, ops))
                continue
            project_id = getattr(node, "project_id", "") or ""
            key = (id(client), project_id)
            group = groups.get(key)
            if group is None:
                group = (client, project_id, [])
                groups[key] = group
            group[2].append((node, ops))
        return list(groups.values()), legacy_jobs

    def next_route_chunk(self, jobs, start: int, max_nodes: int, max_bytes: int):
        """Build the next RouteInstall chunk from *jobs* starting at *start*.

        Returns:
            A tuple of (chunk, next_index), where chunk is a list of
            (node, RouteBatch) pairs.
        """
        chunk: list[tuple[Any, Any]] = []
        total_size = 0
        index = start
        max_nodes = max(1, max_nodes)
        while index < len(jobs) and len(chunk) < max_nodes:
            node, ops = jobs[index]
            batch = self.build_route_batch(ops)
            size = batch.ByteSize() + len(node.name.encode("utf-8")) + 16
            if chunk and total_size + size > max_bytes:
                break
            if not chunk and size > max_bytes:
                logging.warning(
                    "Route batch for %s is %d bytes, above the chunk limit",
                    node.name,
                    size,
                )
            chunk.append((node, batch))
            total_size += size
            index += 1
        return chunk, index

    def build_route_batch(self, ops: list[RouteOperation]) -> netem_pb2.RouteBatch:  # type: ignore[name-defined]
        """Convert typed route operations into a protobuf RouteBatch."""
        batch = netem_pb2.RouteBatch()
        for op in ops:
            spec = batch.routes.add()
            spec.action = (
                netem_pb2.ROUTE_DELETE
                if op.action == "del"
                else netem_pb2.ROUTE_REPLACE
            )
            spec.destination = op.destination
            spec.gateway = op.gateway
            spec.device = op.device
        return batch

    def send_route_install(
        self,
        client,
        project_id: str,
        chunk,
        slow_duration_micros: int = SLOW_ROUTE_DURATION_MICROS,
    ) -> _BatchOutcome:
        """Send one RouteInstall request and interpret its per-node results."""
        request = netem_pb2.RouteInstallRequest(
            prjId=project_id,
            maxConcurrency=0,
        )
        for node, batch in chunk:
            request.nodes[node.name].CopyFrom(batch)

        try:
            response = client.RouteInstall(request)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise _RouteInstallUnsupported() from exc
            raise

        if (
            response.HasField("status")
            and response.status.code == netem_pb2.StatusCode.ERROR
        ):
            raise RuntimeError(f"RouteInstall failed: {response.status.error}")

        outcome = _BatchOutcome()
        for node, _batch in chunk:
            result = response.results.get(node.name)
            if result is None:
                outcome.error_nodes.add(node.name)
                logging.error("RouteInstall returned no result for %s", node.name)
                continue
            if (
                not result.HasField("status")
                or result.status.code == netem_pb2.StatusCode.ERROR
            ):
                error = (
                    result.status.error
                    if result.HasField("status")
                    else "missing status"
                )
                outcome.error_nodes.add(node.name)
                logging.error(f"RouteInstall failed on {node.name}: {error}")
                continue

            outcome.ok_nodes.add(node.name)
            outcome.applied += result.applied
            if result.durationMicros:
                outcome.durations.append(result.durationMicros)
                if result.durationMicros >= slow_duration_micros:
                    logging.warning(
                        "RouteInstall slow on %s: %.1f ms for %d routes",
                        node.name,
                        result.durationMicros / 1000.0,
                        result.applied,
                    )
        return outcome

    def install_route_batches(
        self,
        batches,
        max_workers: int = 128,
        *,
        exec_batch,
        format_operation,
        max_nodes: int = MAX_ROUTE_NODES_PER_REQUEST,
        max_bytes: int = MAX_ROUTE_REQUEST_BYTES,
        slow_duration_micros: int = SLOW_ROUTE_DURATION_MICROS,
    ) -> _BatchOutcome:
        """Install route batches through RouteInstall with legacy fallback.

        Args:
            batches: Mapping of node object (or node name) to its list of
                RouteOperation.
            max_workers: Maximum number of worker threads for the legacy
                per-node fallback path.
            exec_batch: Callable executing a command batch on a node.
            format_operation: Callable formatting a RouteOperation as a string.
            max_nodes: Maximum nodes per RouteInstall request.
            max_bytes: Maximum serialized bytes per RouteInstall request.
            slow_duration_micros: Threshold for the slow-batch warning.

        Returns:
            Per-node outcome.  Per-node errors are logged and do not stop the
            remaining nodes.  Non-UNIMPLEMENTED gRPC errors propagate.
        """
        outcome = _BatchOutcome()
        jobs = []
        for key, ops in batches.items():
            if not ops:
                continue
            node = self.resolve_route_batch_node(key)
            if node is None:
                name = key if isinstance(key, str) else getattr(key, "name", str(key))
                outcome.error_nodes.add(name)
                logging.error(f"RouteInstall: unknown node {name}")
                continue
            jobs.append((node, list(ops)))
        if not jobs:
            return outcome

        legacy_jobs = []
        if self._route_install_supported:
            groups, legacy_jobs = self.partition_route_batches(jobs)
            fallback = False
            for group_index, (client, project_id, group_jobs) in enumerate(groups):
                index = 0
                while index < len(group_jobs):
                    chunk, next_index = self.next_route_chunk(
                        group_jobs, index, max_nodes, max_bytes
                    )
                    try:
                        outcome.merge(
                            self.send_route_install(
                                client,
                                project_id,
                                chunk,
                                slow_duration_micros,
                            )
                        )
                    except _RouteInstallUnsupported:
                        self._route_install_supported = False
                        logging.warning(
                            "RouteInstall is not implemented by the gonetem "
                            "server; using the legacy per-node NodeRun route path."
                        )
                        legacy_jobs.extend(group_jobs[index:])
                        for _, _, remaining in groups[group_index + 1 :]:
                            legacy_jobs.extend(remaining)
                        fallback = True
                        break
                    index = next_index
                if fallback:
                    break
        else:
            legacy_jobs = jobs

        if legacy_jobs:
            outcome.merge(
                self.apply_legacy_batches(
                    legacy_jobs,
                    max_workers=max_workers,
                    exec_batch=exec_batch,
                    format_operation=format_operation,
                )
            )

        if outcome.durations:
            logging.info(
                "RouteInstall node timings: n=%d p50=%dus p95=%dus",
                len(outcome.durations),
                _percentile(outcome.durations, 50),
                _percentile(outcome.durations, 95),
            )
        logging.info(
            "Route updates complete. ok=%d error=%d routes=%d",
            len(outcome.ok_nodes),
            len(outcome.error_nodes),
            outcome.applied,
        )
        return outcome

    def apply_legacy_batches(
        self, jobs, max_workers: int = 128, *, exec_batch, format_operation
    ) -> _BatchOutcome:
        """Apply route operations with the legacy per-node NodeRun path."""
        outcome = _BatchOutcome()
        if not jobs:
            return outcome

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {}
            for node, ops in jobs:
                lines = [format_operation(op) for op in ops]
                future = pool.submit(exec_batch, node, lines)
                future_map[future] = (node, len(ops))

            for fut in as_completed(future_map):
                node, route_count = future_map[fut]
                try:
                    fut.result()
                except Exception as exc:
                    outcome.error_nodes.add(node.name)
                    logging.exception(f"Route update failed on {node.name}: {exc}")
                else:
                    outcome.ok_nodes.add(node.name)
                    outcome.applied += route_count
        return outcome
