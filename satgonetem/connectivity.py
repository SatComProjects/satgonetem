"""Typed full-mesh connectivity checks built on the ping flow API."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from satgonetem.traffic.flow_scheduler import FlowScheduler
from satgonetem.traffic.ping_utils import PingConfig, PingFlow

__all__ = [
    "ConnectivityMatrix",
    "ConnectivityResult",
    "build_connectivity_matrix",
]


@dataclass(frozen=True)
class ConnectivityResult:
    """One ordered source/destination ping result."""

    source: str
    destination: str
    reachable: bool
    packets_transmitted: int
    packets_received: int
    packet_loss_percent: float
    rtt_min_ms: float
    rtt_avg_ms: float
    rtt_max_ms: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


@dataclass
class ConnectivityMatrix:
    """Results of an ordered all-pairs connectivity check."""

    results: list[ConnectivityResult]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary and result list."""
        reachable = sum(1 for result in self.results if result.reachable)
        return {
            "summary": {
                "pairs": len(self.results),
                "reachable": reachable,
                "unreachable": len(self.results) - reachable,
            },
            "results": [result.to_dict() for result in self.results],
        }

    def to_json(self, **kwargs: Any) -> str:
        """Serialize the matrix to JSON."""
        return json.dumps(self.to_dict(), **kwargs)

    def print_summary(self) -> None:
        """Print a short human-readable summary."""
        data = self.to_dict()["summary"]
        print(
            f"Connectivity: {data['reachable']}/{data['pairs']} pairs reachable "
            f"({data['unreachable']} unreachable)"
        )


def build_connectivity_matrix(
    nodes: Sequence[Any],
    *,
    config: PingConfig | None = None,
    max_workers: int = 128,
    flow_timeout_sec: float = 60.0,
    flow_factory: Callable[[Any, Any, PingConfig], Any] = PingFlow,
    scheduler_factory: Callable[..., Any] = FlowScheduler,
) -> ConnectivityMatrix:
    """Ping every ordered node pair and return typed results.

    Args:
        nodes: Nodes to test. Duplicate names are ignored after the first
            occurrence.
        config: Ping configuration. Defaults to ten fast, small pings.
        max_workers: Maximum concurrent flows passed to the scheduler.
        flow_timeout_sec: Per-flow timeout passed to the scheduler.
        flow_factory: Injection point for tests; defaults to :class:`PingFlow`.
        scheduler_factory: Injection point for tests; defaults to
            :class:`FlowScheduler`.

    Returns:
        A :class:`ConnectivityMatrix` with one result per ordered pair.
    """
    ping_config = config or PingConfig(
        count=10, interval_sec=0.01, preload=1, packet_size=64
    )

    unique_nodes = []
    seen: set[str] = set()
    for node in nodes:
        name = str(getattr(node, "name", node))
        if name in seen:
            continue
        seen.add(name)
        unique_nodes.append(node)

    flows = []
    for index, source in enumerate(unique_nodes):
        for destination in unique_nodes:
            if source is destination:
                continue
            flows.append(
                flow_factory(
                    source,
                    destination,
                    ping_config,
                )
            )
            flows[-1].delay = index * ping_config.interval_sec * ping_config.count

    if not flows:
        return ConnectivityMatrix(results=[])

    scheduler = scheduler_factory(
        flows,
        max_workers=max_workers,
        debug=False,
        flow_timeout_sec=flow_timeout_sec,
    )
    scheduler.run()
    scheduler.join()

    status_fn = getattr(scheduler, "status", None)
    if callable(status_fn):
        from satgonetem.traffic.flow_scheduler import FlowSchedulerStatus

        if status_fn() == FlowSchedulerStatus.ERROR:
            errors_fn = getattr(scheduler, "errors", None)
            if callable(errors_fn):
                errors_fn()
            raise RuntimeError("Connectivity probe scheduler failed")

    results: list[ConnectivityResult] = []
    for flow in flows:
        source_name = str(getattr(getattr(flow, "source", None), "name", ""))
        destination_name = str(getattr(getattr(flow, "destination", None), "name", ""))
        try:
            try:
                parsed = scheduler.results(flow)
            except KeyError:
                flow_results = getattr(flow, "results", None)
                if callable(flow_results):
                    parsed = flow_results()
                else:
                    raise
            results.append(
                ConnectivityResult(
                    source=source_name,
                    destination=destination_name,
                    reachable=bool(parsed.reachable),
                    packets_transmitted=int(parsed.packets_transmitted),
                    packets_received=int(parsed.packets_received),
                    packet_loss_percent=float(parsed.packet_loss_percent),
                    rtt_min_ms=float(parsed.rtt_min_ms),
                    rtt_avg_ms=float(parsed.rtt_avg_ms),
                    rtt_max_ms=float(parsed.rtt_max_ms),
                )
            )
        except Exception as exc:  # noqa: BLE001 - report per pair
            results.append(
                ConnectivityResult(
                    source=source_name,
                    destination=destination_name,
                    reachable=False,
                    packets_transmitted=0,
                    packets_received=0,
                    packet_loss_percent=100.0,
                    rtt_min_ms=0.0,
                    rtt_avg_ms=0.0,
                    rtt_max_ms=0.0,
                    error=str(exc),
                )
            )

    return ConnectivityMatrix(results=results)
