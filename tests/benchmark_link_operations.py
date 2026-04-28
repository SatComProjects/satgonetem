"""Benchmark gRPC link operations (LinkUpdate, LinkAdd, LinkDel) against GoNetem.

Creates a 30x30 Walker Star constellation (900 satellites), starts GoNetem,
then opens its own gRPC channel and calls LinkUpdate / LinkAdd / LinkDel
sequentially, measuring per-request and total latency.

Scenarios (each repeated RUNS=5 times, for N in [10, 100, 1000]):
  - Link Update: LinkUpdate for N randomly selected existing links.
  - Link Creation: LinkAdd for N unconnected satellite pairs, then
    LinkDel to restore state.

Results are appended to tests/benchmark.txt.

Requirements:
    A GoNetem server must be reachable at localhost:10110 before running.

Run:
    python tests/benchmark_link_operations.py
    pytest tests/benchmark_link_operations.py -v -s
"""

import datetime
import logging
import random
import statistics
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Tuple

import grpc

from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty

import satgonetem.proto.netem_pb2 as netem_pb2
import satgonetem.proto.netem_pb2_grpc as netem_grpc

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import GroundObjectFile, GroundStationEntry


SCRIPT_DIR = Path(__file__).resolve().parent
BENCHMARK_FILE = SCRIPT_DIR / "benchmark.txt"

GONETEM_SERVER = "localhost:10110"
ISL_CAPACITY_KBPS = 100_000
ISL_DISTANCE_M = 1_200_000
ISL_DELAY_MS = max(int(ISL_DISTANCE_M / 299_792_458 * 1000), 1)

RUNS = 5
LINK_COUNTS = [10, 100, 1000]
PLANES = 30
SATS_PER_PLANE = 30


class RequestTiming(NamedTuple):
    """Timing for a sequential batch of gRPC requests.

    Attributes:
        n: Number of requests sent.
        per_request_ms: Individual round-trip times in ms, one per request.
        total_ms: Wall-clock time for the entire batch in ms.
    """

    n: int
    per_request_ms: List[float]
    total_ms: float

    @property
    def mean_ms(self) -> float:
        """Mean per-request time in ms."""
        return statistics.mean(self.per_request_ms) if self.per_request_ms else 0.0

    @property
    def stdev_ms(self) -> float:
        """Standard deviation of per-request times in ms."""
        return (
            statistics.stdev(self.per_request_ms)
            if len(self.per_request_ms) > 1
            else 0.0
        )

    @property
    def min_ms(self) -> float:
        """Minimum per-request time in ms."""
        return min(self.per_request_ms) if self.per_request_ms else 0.0

    @property
    def max_ms(self) -> float:
        """Maximum per-request time in ms."""
        return max(self.per_request_ms) if self.per_request_ms else 0.0


def _build_simulation_property() -> SimulationProperty:
    """Build a 30x30 Walker Star SimulationProperty with 900 satellites.

    Uses 1500 km ISL range and five European ground stations to produce
    at least 1000 active links.

    Returns:
        SimulationProperty ready to pass to TopologyManager.from_satcom().
    """
    ground_stations = [
        GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
        GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
        GroundStationEntry(2, "Paris", 48.856, 2.352, 0.035),
        GroundStationEntry(3, "Rome", 41.902, 12.496, 0.021),
        GroundStationEntry(4, "Madrid", 40.416, -3.703, 0.667),
    ]
    gs_file = GroundObjectFile("Ground Stations", ground_stations)
    data_file = gs_file.write("/tmp")

    ground_object_property = GroundObjectProperty(
        identifier=gs_file.identifier,
        data_file=data_file,
        type="ground_station",
        connectivity_properties=GroundConnectivityProperty(
            ground_to_space_connections_strategy="best-angle-until-disconnection",
            elevation_above_horizon=10,
            maximum_satellite_range_distance=1500.0,
            shell_white_lists=["LEO"],
            maximum_connected_satellites=3,
        ),
    )

    shell = WalkerShellProperty(
        type="star",
        constellation_property=WalkerConstellationProperty(
            identifier="LEO",
            amount_of_orbit_plane=PLANES,
            amount_of_satellite_per_orbit_plane=SATS_PER_PLANE,
            inclination=86.4,
            mean_revolution_per_day=14.35,
            phase_difference_between_satellites=True,
        ),
        orbital_connectivity_property=OrbitalConnectivityProperty(
            adjacent_inter_satellite_shifting=0,
            maximum_inter_satellite_count=4,
            maximum_inter_satellite_range_distance=1500.0,
            maximum_ground_station_range=1200.0,
            maximum_user_terminal_range=1000.0,
            maximum_connected_ground_object=10000,
            maximum_connected_user_terminal=500,
            maximum_connected_ground_station=10,
        ),
        ground_object_white_list=["Ground Stations"],
    )

    return SimulationProperty(
        simulation_name="BenchmarkConstellation30x30",
        start_date="01/01/2024 00:00:00",
        end_date="01/01/2024 00:01:00",
        ground_objects_properties=[ground_object_property],
        walker_shells=[shell],
    )


def _make_link_request(
    prj_id: str,
    src_name: str,
    src_id: int,
    dst_name: str,
    dst_id: int,
    delay_ms: int,
    rate_kbps: int,
) -> netem_pb2.LinkRequest:
    """Build a LinkRequest proto message for a single link.

    Args:
        prj_id: GoNetem project ID.
        src_name: Source node name (e.g. "Sat0").
        src_id: Source node integer ID.
        dst_name: Target node name (e.g. "Sat1").
        dst_id: Target node integer ID.
        delay_ms: Propagation delay in milliseconds.
        rate_kbps: Link rate in kbps applied to both peers.

    Returns:
        Populated LinkRequest ready to pass to LinkAdd/LinkUpdate/LinkDel.
    """
    qos = netem_pb2.LinkConfig.QoSConfig(delay=delay_ms, rate=rate_kbps)
    cfg = netem_pb2.LinkConfig(
        peer1=f"{src_name}.{dst_id}",
        peer2=f"{dst_name}.{src_id}",
        peer1qos=qos,
        peer2qos=qos,
    )
    return netem_pb2.LinkRequest(prjId=prj_id, link=cfg)


def _send_sequential(
    requests: List[netem_pb2.LinkRequest],
    rpc_method: Callable,
    label: str,
) -> RequestTiming:
    """Send requests one at a time and record per-request and total time.

    Args:
        requests: Pre-built LinkRequest messages.
        rpc_method: Bound stub method (e.g. stub.LinkUpdate).
        label: Short label for progress output.

    Returns:
        RequestTiming with per-request latencies and total wall-clock time.
    """
    per_req: List[float] = []
    milestone = max(1, len(requests) // 5)

    batch_start = time.perf_counter()
    for i, req in enumerate(requests):
        t0 = time.perf_counter()
        rpc_method(req)
        per_req.append((time.perf_counter() - t0) * 1000)
        if (i + 1) % milestone == 0:
            print(
                f"    [{label}] {i + 1}/{len(requests)}" f"  last={per_req[-1]:.2f}ms",
                flush=True,
            )

    total_ms = (time.perf_counter() - batch_start) * 1000
    return RequestTiming(n=len(requests), per_request_ms=per_req, total_ms=total_ms)


def _build_update_requests(
    prj_id: str, topology: TopologyManager, n: int
) -> List[netem_pb2.LinkRequest]:
    """Build LinkUpdate requests for n randomly selected existing links.

    Args:
        prj_id: GoNetem project ID.
        topology: Initialized TopologyManager.
        n: Number of links to select.

    Returns:
        List of n LinkRequest messages.
    """
    selected = random.sample(list(topology.links.values()), n)
    return [
        _make_link_request(
            prj_id=prj_id,
            src_name=link.source.name,
            src_id=link.source.id,
            dst_name=link.target.name,
            dst_id=link.target.id,
            delay_ms=max(int(link.delay), 1),
            rate_kbps=ISL_CAPACITY_KBPS,
        )
        for link in selected
    ]


def _find_unconnected_pairs(topology: TopologyManager, n: int) -> List[Tuple]:
    """Find n satellite pairs with no existing link between them.

    Args:
        topology: Initialized TopologyManager.
        n: Number of pairs to find.

    Returns:
        List of (satellite1, satellite2) tuples, length n.

    Raises:
        ValueError: If n pairs cannot be found within the attempt budget.
    """
    satellites = list(topology.satellites.values())
    existing_keys = set(topology.links.keys())
    chosen_keys: set = set()
    pairs: list = []

    print(f"  searching for {n} unconnected satellite pairs...", flush=True)
    max_attempts = n * 200
    milestone = max(1, n // 5)

    for attempt in range(max_attempts):
        if len(pairs) == n:
            break
        s1, s2 = random.sample(satellites, 2)
        key = frozenset([s1.name, s2.name])
        if key not in existing_keys and key not in chosen_keys:
            pairs.append((s1, s2))
            chosen_keys.add(key)
            if len(pairs) % milestone == 0:
                print(f"  found {len(pairs)}/{n} (attempt {attempt + 1})", flush=True)

    if len(pairs) < n:
        raise ValueError(
            f"Could not find {n} unconnected satellite pairs after {max_attempts} attempts."
        )
    return pairs


def _build_creation_requests(
    prj_id: str, topology: TopologyManager, n: int
) -> Tuple[List[netem_pb2.LinkRequest], List[netem_pb2.LinkRequest]]:
    """Build LinkAdd and matching LinkDel requests for n unconnected pairs.

    Args:
        prj_id: GoNetem project ID.
        topology: Initialized TopologyManager.
        n: Number of synthetic links.

    Returns:
        Tuple of (add_requests, del_requests), each length n.
    """
    pairs = _find_unconnected_pairs(topology, n)
    add_reqs, del_reqs = [], []
    for s1, s2 in pairs:
        req = _make_link_request(
            prj_id=prj_id,
            src_name=s1.name,
            src_id=s1.id,
            dst_name=s2.name,
            dst_id=s2.id,
            delay_ms=ISL_DELAY_MS,
            rate_kbps=ISL_CAPACITY_KBPS,
        )
        add_reqs.append(req)
        del_reqs.append(req)
    return add_reqs, del_reqs


def _run_update_scenario(
    stub: netem_grpc.NetemStub,
    prj_id: str,
    topology: TopologyManager,
    n: int,
) -> List[RequestTiming]:
    """Run RUNS repetitions of the link-update benchmark for n links.

    Args:
        stub: Connected NetemStub.
        prj_id: GoNetem project ID.
        topology: Initialized TopologyManager.
        n: Number of links to update per run.

    Returns:
        List of RequestTiming, one per run.
    """
    results = []
    for run in range(1, RUNS + 1):
        print(f"\n  [update n={n}] run {run}/{RUNS}", flush=True)
        reqs = _build_update_requests(prj_id, topology, n)
        timing = _send_sequential(reqs, stub.LinkUpdate, f"update n={n}")
        results.append(timing)
        print(
            f"  -> total={timing.total_ms:.2f}ms"
            f"  mean/req={timing.mean_ms:.2f}ms"
            f"  min={timing.min_ms:.2f}ms"
            f"  max={timing.max_ms:.2f}ms",
            flush=True,
        )
    return results


def _run_creation_scenario(
    stub: netem_grpc.NetemStub,
    prj_id: str,
    topology: TopologyManager,
    n: int,
) -> List[RequestTiming]:
    """Run RUNS repetitions of the link-creation benchmark for n links.

    Each run finds fresh unconnected pairs, sends LinkAdd sequentially,
    then cleans up with LinkDel (cleanup is not timed).

    Args:
        stub: Connected NetemStub.
        prj_id: GoNetem project ID.
        topology: Initialized TopologyManager.
        n: Number of synthetic links per run.

    Returns:
        List of RequestTiming for LinkAdd, one per run.
    """
    results = []
    for run in range(1, RUNS + 1):
        print(f"\n  [creation n={n}] run {run}/{RUNS}", flush=True)
        add_reqs, del_reqs = _build_creation_requests(prj_id, topology, n)

        print(f"  sending {n} LinkAdd requests...", flush=True)
        timing = _send_sequential(add_reqs, stub.LinkAdd, f"add n={n}")
        results.append(timing)
        print(
            f"  -> total={timing.total_ms:.2f}ms"
            f"  mean/req={timing.mean_ms:.2f}ms"
            f"  min={timing.min_ms:.2f}ms"
            f"  max={timing.max_ms:.2f}ms",
            flush=True,
        )

        print(f"  sending {n} LinkDel requests (cleanup)...", flush=True)
        _send_sequential(del_reqs, stub.LinkDel, f"del n={n}")

    return results


def _summarize_runs(results: List[RequestTiming]) -> Dict[str, float]:
    """Compute cross-run statistics for a list of RequestTiming objects.

    Args:
        results: Per-run RequestTiming objects.

    Returns:
        Dict with mean_total_ms, stdev_total_ms, mean_per_req_ms keys.
    """
    totals = [r.total_ms for r in results]
    per_reqs = [r.mean_ms for r in results]
    return {
        "mean_total_ms": round(statistics.mean(totals), 3),
        "stdev_total_ms": round(
            statistics.stdev(totals) if len(totals) > 1 else 0.0, 3
        ),
        "mean_per_req_ms": round(statistics.mean(per_reqs), 3),
    }


def _write_results(
    fh,
    label: str,
    n: int,
    results: List[RequestTiming],
) -> None:
    """Write per-run and summary lines for one scenario to an open file.

    Args:
        fh: Open writable file handle.
        label: Scenario name.
        n: Number of links per run.
        results: Per-run timing data.
    """
    fh.write(f"\n  {label}  n={n}\n")
    fh.write(
        f"    {'run':>3}  {'total_ms':>12}  {'mean/req_ms':>13}"
        f"  {'min_ms':>10}  {'max_ms':>10}  {'stdev_ms':>10}\n"
    )
    for i, r in enumerate(results, start=1):
        fh.write(
            f"    {i:3d}  {r.total_ms:10.3f}ms  {r.mean_ms:11.3f}ms"
            f"  {r.min_ms:8.3f}ms  {r.max_ms:8.3f}ms  {r.stdev_ms:8.3f}ms\n"
        )
    s = _summarize_runs(results)
    fh.write(
        f"    summary"
        f"  mean_total={s['mean_total_ms']:.3f}ms"
        f"  stdev_total={s['stdev_total_ms']:.3f}ms"
        f"  mean_per_req={s['mean_per_req_ms']:.3f}ms\n"
    )


def _setup_logging() -> None:
    """Configure root logger to emit INFO to stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> None:
    """Run link update and creation benchmarks against a live GoNetem server."""
    _setup_logging()

    print("Building 30x30 constellation...", flush=True)
    sim_prop = _build_simulation_property()
    topology = TopologyManager.from_satcom(sim_prop, NetworkConfig())

    n_links = len(topology.links)
    print(
        f"Topology ready: {len(topology.satellites)} satellites, {n_links} links",
        flush=True,
    )
    if n_links < max(LINK_COUNTS):
        raise RuntimeError(
            f"Constellation produced {n_links} links; need at least {max(LINK_COUNTS)}."
        )

    print("Starting GoNetem...", flush=True)
    topology.start_gonetem()
    prj_id: str = topology.direct_launcher._request.id
    print(f"GoNetem started. Project ID: {prj_id}", flush=True)

    channel = grpc.insecure_channel(GONETEM_SERVER)
    stub = netem_grpc.NetemStub(channel)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    update_results: Dict[int, List[RequestTiming]] = {}
    creation_results: Dict[int, List[RequestTiming]] = {}

    try:
        print("\n=== Link Update Benchmark ===", flush=True)
        for n in LINK_COUNTS:
            update_results[n] = _run_update_scenario(stub, prj_id, topology, n)

        print("\n=== Link Creation Benchmark ===", flush=True)
        for n in LINK_COUNTS:
            creation_results[n] = _run_creation_scenario(stub, prj_id, topology, n)

    finally:
        channel.close()
        print("\nStopping GoNetem...", flush=True)
        topology.stop_gonetem()
        print("Done.", flush=True)

    with BENCHMARK_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== Benchmark run {ts} ===\n")
        fh.write(f"Constellation: {PLANES}x{SATS_PER_PLANE} Walker Star  RUNS={RUNS}\n")
        fh.write("\nLink Update\n")
        for n in LINK_COUNTS:
            _write_results(fh, "update", n, update_results[n])
        fh.write("\nLink Creation\n")
        for n in LINK_COUNTS:
            _write_results(fh, "creation", n, creation_results[n])

    print(f"\nResults written to {BENCHMARK_FILE}", flush=True)


def test_link_operations_benchmark() -> None:
    """Pytest entry point: runs the full link benchmark suite.

    Delegates entirely to main(), which handles topology setup, GoNetem
    lifecycle, gRPC benchmarking, and result writing.
    """
    main()


if __name__ == "__main__":
    main()
