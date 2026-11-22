#!/usr/bin/env python3
"""Repeated end-to-end check of a 10x10 Walker constellation on GoNetEm.

Builds a 10-plane x 10-satellite Walker constellation with the five default
European ground stations, launches it on GoNetEm, assigns the planned IPv4
addresses, installs static routes, and then:

1. sends a throwaway one-packet ARP warm-up ping per pair and discards it;
2. pings every directed ground-station pair (loopback to loopback);
3. compares the measured RTT with the propagation distance of the routes the
   static daemon installs, ``(forward_km + reverse_km) / c``;
4. advances the simulation one step and repeats.

By default the initial topology is checked first, then ten 5-minute simulation
updates are applied (11 rounds total), so link and route changes are exercised.
Pass ``--warmup-packets 0`` to disable the ARP warm-up. A GoNetEm server must
be reachable at ``NetworkConfig.gonetem_server`` (default
``localhost:10110``) and Docker must be able to run the 105 node containers.

Examples::

    .venv/bin/python scripts/verify_10x10_connectivity.py
    .venv/bin/python scripts/verify_10x10_connectivity.py --transport docker
"""

from __future__ import annotations

import argparse
import itertools
import time
from dataclasses import dataclass

import networkx as nx

from satgonetem.models.node import Node
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.traffic import PingConfig, PingFlow, PingStatus
from satgonetem.utils.project_builder import create_test_project

#: Speed of light in km/ms.
SPEED_OF_LIGHT_KM_PER_MS = 299.792458


@dataclass
class Metrics:
    """RTT expected from the graph and from the configured netem delays."""

    expected_ms: float
    netem_ms: float
    tolerance_ms: float
    forward_km: float
    reverse_km: float
    forward_hops: int
    reverse_hops: int


@dataclass
class Probe:
    """One directed ground-station pair."""

    source: Node
    destination: Node
    metrics: Metrics | None = None
    flow: PingFlow | None = None
    error: str | None = None


def _build_topology(args: argparse.Namespace) -> TopologyManager:
    """Build and initialise the constellation without launching containers."""
    project = create_test_project(simulation_name=f"Verify10x10_{int(time.time())}")
    shell = project.walker_shells[0]
    shell.constellation_property.amount_of_orbit_plane = args.planes
    shell.constellation_property.amount_of_satellite_per_orbit_plane = (
        args.sats_per_plane
    )
    config = NetworkConfig(update_time=args.update_time)
    if args.gonetem_server:
        config.gonetem_server = args.gonetem_server
    return TopologyManager.from_satcom(project, config)


def _node_token(topology: TopologyManager, node_id: int) -> str:
    """Return the Sat*/Gnd* token used as a link key for a graph node."""
    if node_id in topology.satellites:
        return f"Sat{node_id}"
    if node_id in topology.ground_stations:
        return f"Gnd{node_id}"
    raise KeyError(f"graph node {node_id} is not a satellite or ground station")


def _route_path(graph: nx.Graph, source_id: int, destination_id: int) -> list[int]:
    """Reproduce the path the static routing daemon installs.

    The daemon roots a Dijkstra tree at the destination and picks the first
    predecessor as the next hop. Following that chain from the source matches
    the installed route, including tie-breaking.
    """
    predecessors, _ = nx.dijkstra_predecessor_and_distance(
        graph, source=destination_id, weight="weight"
    )
    path = [source_id]
    node = source_id
    while node != destination_id:
        next_hops = predecessors.get(node, [])
        if not next_hops:
            raise nx.NetworkXNoPath(f"no path {source_id} -> {destination_id}")
        node = next_hops[0]
        path.append(node)
        if len(path) > graph.number_of_nodes():
            raise nx.NetworkXUnfeasible(
                f"predecessor cycle while routing {source_id} -> {destination_id}"
            )
    return path


def _path_metrics(
    topology: TopologyManager,
    graph: nx.Graph,
    source: Node,
    destination: Node,
) -> Metrics:
    """Return expected RTT and netem delay for one directed pair."""
    forward = _route_path(graph, source.id, destination.id)
    reverse = _route_path(graph, destination.id, source.id)

    def measure(path: list[int]) -> tuple[float, float]:
        distance_km = 0.0
        netem_ms = 0.0
        for u, v in zip(path, path[1:], strict=False):
            distance_km += float(graph[u][v]["distance_km"])
            link = topology.links.get(
                frozenset((_node_token(topology, u), _node_token(topology, v)))
            )
            if link is None:
                raise KeyError(f"no Link object for graph edge {u}-{v}")
            netem_ms += max(int(link.delay), 1)
        return distance_km, netem_ms

    forward_km, forward_netem = measure(forward)
    reverse_km, reverse_netem = measure(reverse)
    return Metrics(
        expected_ms=(forward_km + reverse_km) / SPEED_OF_LIGHT_KM_PER_MS,
        netem_ms=forward_netem + reverse_netem,
        # Integer netem delays differ from the exact propagation time by up to
        # 1 ms per link; that is the only systematic rounding gap.
        tolerance_ms=float(len(forward) + len(reverse) - 2),
        forward_km=forward_km,
        reverse_km=reverse_km,
        forward_hops=len(forward) - 1,
        reverse_hops=len(reverse) - 1,
    )


def _prepare_round(
    topology: TopologyManager,
    ground_stations: list[Node],
) -> list[Probe]:
    """Create one probe per directed ground-station pair."""
    graph = topology.get_current_graph()
    probes: list[Probe] = []
    for source, destination in itertools.permutations(ground_stations, 2):
        probe = Probe(source=source, destination=destination)
        try:
            probe.metrics = _path_metrics(topology, graph, source, destination)
        except (KeyError, nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            probe.error = f"no usable graph path: {exc}"
        probes.append(probe)
    return probes


def _start_pings(
    topology: TopologyManager,
    probes: list[Probe],
    ping_config: PingConfig,
) -> None:
    """Start one ping flow per probe that has a graph path."""
    for probe in probes:
        if probe.metrics is not None:
            probe.flow = topology.ping(probe.source, probe.destination, ping_config)[0]


def _wait_for_flows(probes: list[Probe], timeout_sec: float) -> None:
    """Join every ping flow, stopping any that outlive the round deadline."""
    deadline = time.monotonic() + timeout_sec
    for probe in probes:
        if probe.flow is not None:
            probe.flow.join(max(0.0, deadline - time.monotonic()))
    for probe in probes:
        if probe.flow is not None and probe.flow.is_alive():
            probe.flow.stop()


def _warm_up(
    topology: TopologyManager,
    probes: list[Probe],
    warmup_config: PingConfig,
    timeout_sec: float,
) -> None:
    """Prime ARP with a throwaway ping before the measured run."""
    _start_pings(topology, probes, warmup_config)
    _wait_for_flows(probes, timeout_sec)
    for probe in probes:
        if probe.flow is not None:
            probe.flow.discard_result()
            probe.flow = None


def _evaluate_round(probes: list[Probe], args: argparse.Namespace) -> list[str]:
    """Check every probe and return human-readable failures."""
    failures: list[str] = []
    rtts: list[float] = []
    deltas: list[float] = []
    for probe in probes:
        label = f"{probe.source.name} -> {probe.destination.name}"
        error = probe.error
        ping = None
        if error is None and probe.flow is None:
            error = "ping flow was not started"
        elif error is None:
            assert probe.flow is not None
            if probe.flow.status() != PingStatus.DONE:
                error = f"ping status is {probe.flow.status().value}"
            else:
                try:
                    ping = probe.flow.results()
                except Exception as exc:  # noqa: BLE001 - report parse failure
                    error = f"could not parse ping result: {exc}"
                else:
                    error = _check_ping(probe, ping, args)
        if error is not None:
            detail = ""
            if probe.metrics is not None:
                metrics = probe.metrics
                detail = (
                    f" (expected {metrics.expected_ms:.2f} ms, "
                    f"netem {metrics.netem_ms:.2f} ms, "
                    f"km {metrics.forward_km:.0f}/{metrics.reverse_km:.0f}, "
                    f"hops {metrics.forward_hops}/{metrics.reverse_hops})"
                )
            failures.append(f"{label}: {error}{detail}")
            continue
        assert ping is not None
        rtts.append(ping.rtt_avg_ms)
        assert probe.metrics is not None
        deltas.append(abs(ping.rtt_avg_ms - probe.metrics.expected_ms))
    if rtts:
        print(
            f"  {len(rtts)}/{len(probes)} pairs OK, "
            f"RTT {min(rtts):.2f}-{max(rtts):.2f} ms, "
            f"max |measured-expected| {max(deltas):.2f} ms",
            flush=True,
        )
    else:
        print(f"  0/{len(probes)} pairs OK", flush=True)
    for failure in failures:
        print(f"  FAIL {failure}", flush=True)
    return failures


def _check_ping(probe: Probe, ping, args: argparse.Namespace) -> str | None:
    """Return a failure reason for one completed ping, or None."""
    metrics = probe.metrics
    assert metrics is not None
    if ping.packet_loss_percent > args.max_loss_pct:
        return (
            f"packet loss {ping.packet_loss_percent:.1f}% "
            f"({ping.packets_received}/{ping.packets_transmitted})"
        )

    tolerance_ms = (
        max(args.abs_tolerance_ms, args.rel_tolerance * metrics.expected_ms)
        + metrics.tolerance_ms
    )
    delta_ms = ping.rtt_avg_ms - metrics.expected_ms
    if abs(delta_ms) > tolerance_ms:
        return (
            f"RTT {ping.rtt_avg_ms:.2f} ms vs graph-distance expectation "
            f"{metrics.expected_ms:.2f} ms ({delta_ms:+.2f} ms, "
            f"allowed {tolerance_ms:.2f} ms)"
        )

    netem_tolerance_ms = max(
        args.abs_tolerance_ms,
        args.rel_tolerance * metrics.netem_ms,
    )
    netem_delta_ms = ping.rtt_avg_ms - metrics.netem_ms
    if abs(netem_delta_ms) > netem_tolerance_ms:
        return (
            f"RTT {ping.rtt_avg_ms:.2f} ms vs configured netem expectation "
            f"{metrics.netem_ms:.2f} ms ({netem_delta_ms:+.2f} ms, "
            f"allowed {netem_tolerance_ms:.2f} ms)"
        )
    return None


def _run_round(
    topology: TopologyManager,
    ground_stations: list[Node],
    ping_config: PingConfig,
    args: argparse.Namespace,
    round_index: int,
) -> None:
    """Run one round of all-ground-station pings and validate the RTTs."""
    probes = _prepare_round(topology, ground_stations)

    if args.warmup_packets > 0:
        warmup_config = PingConfig(
            count=args.warmup_packets,
            timeout_sec=args.reply_timeout,
            interval_sec=args.interval,
            preload=1,
        )
        _warm_up(topology, probes, warmup_config, args.wait_timeout)

    _start_pings(topology, probes, ping_config)
    _wait_for_flows(probes, args.wait_timeout)
    print(f"Round {round_index}:", flush=True)
    failures = _evaluate_round(probes, args)
    if failures:
        raise RuntimeError(
            f"round {round_index}: {len(failures)}/{len(probes)} pair checks failed"
        )


def run(args: argparse.Namespace) -> int:
    """Execute the verification and return a process exit code."""
    topology = _build_topology(args)
    ground_stations = sorted(
        topology.ground_stations.values(), key=lambda node: node.id
    )
    if len(ground_stations) < 2:
        raise RuntimeError("the topology has fewer than two ground stations")

    print(
        f"Topology: {args.planes} x {args.sats_per_plane} = "
        f"{len(topology.satellites)} satellites, "
        f"{len(ground_stations)} ground stations, "
        f"{len(topology.links)} links"
    )
    print(f"Step: {args.update_time} s ({args.update_time / 60:g} min)")
    pairs = len(ground_stations) * (len(ground_stations) - 1)
    print(f"Rounds: initial + {args.updates} updates, {pairs} directed pairs each")
    if args.warmup_packets > 0:
        print(
            f"Warm-up: {pairs} throwaway ping(s) per round "
            f"({args.warmup_packets} packet(s))"
        )
    ping_config = PingConfig(
        count=args.count,
        timeout_sec=args.reply_timeout,
        interval_sec=args.interval,
        preload=1,
    )

    try:
        start = time.monotonic()
        print("Starting GoNetEm ...", flush=True)
        topology.start_gonetem()
        print(f"GoNetEm started in {time.monotonic() - start:.1f} s", flush=True)

        if args.transport == "docker":
            nodes = list(topology.satellites.values()) + ground_stations
            missing = [node.name for node in nodes if node.container is None]
            if missing:
                raise RuntimeError(
                    "launcher did not attach containers: " + ", ".join(missing)
                )
            for node in nodes:
                node.grpc_client = None
            print("Using docker-py command transport", flush=True)

        start = time.monotonic()
        topology.set_ip_addresses()
        print(
            f"IPv4 addresses assigned in {time.monotonic() - start:.1f} s",
            flush=True,
        )

        start = time.monotonic()
        topology.init_routing(max_workers=args.route_workers, routing_method="static")
        if not topology.get_routing_initiated():
            raise RuntimeError("static routing initialisation failed")
        print(
            f"Static routes installed in {time.monotonic() - start:.1f} s",
            flush=True,
        )

        _run_round(topology, ground_stations, ping_config, args, 0)
        for step in range(1, args.updates + 1):
            start = time.monotonic()
            topology.next_step()
            stats = getattr(topology, "last_link_operation_stats", {})
            print(
                f"Simulation update {step}/{args.updates} "
                f"({time.monotonic() - start:.1f} s) - "
                f"links added={stats.get('added_count', 0)}, "
                f"updated={stats.get('updated_count', 0)}, "
                f"deleted={stats.get('deleted_count', 0)}",
                flush=True,
            )
            _run_round(topology, ground_stations, ping_config, args, step)

        pairs = len(ground_stations) * (len(ground_stations) - 1)
        print(
            f"OK: {args.updates + 1} rounds, {pairs * (args.updates + 1)} "
            "directed ground-station pings passed connectivity and RTT checks",
            flush=True,
        )
        return 0
    finally:
        print("Stopping GoNetEm ...", flush=True)
        try:
            topology.stop_gonetem()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            print(f"cleanup warning: {exc}", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Launch a 10x10 constellation on GoNetEm and repeatedly verify "
            "all-ground-station connectivity and graph-distance RTT."
        )
    )
    parser.add_argument("--planes", type=int, default=10)
    parser.add_argument("--sats-per-plane", type=int, default=10)
    parser.add_argument("--updates", type=int, default=10)
    parser.add_argument(
        "--update-time",
        type=int,
        default=300,
        help="Simulation step in seconds (default: 300 = 5 minutes)",
    )
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--reply-timeout", type=float, default=2.0)
    parser.add_argument("--wait-timeout", type=float, default=60.0)
    parser.add_argument("--rel-tolerance", type=float, default=0.02)
    parser.add_argument("--abs-tolerance-ms", type=float, default=2.0)
    parser.add_argument("--max-loss-pct", type=float, default=0.0)
    parser.add_argument(
        "--warmup-packets",
        type=int,
        default=1,
        help="packets in the throwaway ARP warm-up ping per pair (0 disables)",
    )
    parser.add_argument("--route-workers", type=int, default=64)
    parser.add_argument(
        "--gonetem-server",
        default="",
        help="GoNetEm server address (default: NetworkConfig default, localhost:10110)",
    )
    parser.add_argument(
        "--transport",
        choices=("grpc", "docker"),
        default="grpc",
        help="container command transport (default: grpc)",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 - final verification failure
        print(f"FAIL: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
