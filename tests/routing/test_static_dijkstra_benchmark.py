"""Benchmark networkx vs igraph Dijkstra implementations.

This module is a pytest test file. Run it with:

    pytest tests/routing/test_static_dijkstra_benchmark.py -s

The ``-s`` flag is recommended so the printed timing summary is visible.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import networkx as nx
import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.static_daemon import StaticRoutingDaemon
from satgonetem.utils.ip_utils import IPUtils

if TYPE_CHECKING:
    from satgonetem.models.node import Node


def _make_iface(node_name: str, peer_id: int, peer_iface: Interface) -> Interface:
    """Create an Interface named like 'Gnd0.1' with a bidirectional peer."""
    iface = Interface(name=f"{node_name}.{peer_id}")
    iface.peer = peer_iface
    return iface


def _loopback_ip(node_id: int) -> str:
    """Return a valid loopback-style IPv4 address for a node ID."""
    return f"10.{node_id // 256}.{node_id % 256}.0"


def _link_nodes(a: "Node", b: "Node") -> None:
    """Add cross-referenced interfaces between two nodes."""
    iface_a = Interface(name=f"{a.name}.{b.id}")
    iface_b = Interface(name=f"{b.name}.{a.id}")
    iface_a.peer = iface_b
    iface_b.peer = iface_a
    # Generate valid IPv4s for the point-to-point link.
    iface_a.ipv4, iface_b.ipv4 = IPUtils.get_ipv4_address(
        a.id, b.id, "GroundStation"
    )
    a.interfaces.append(iface_a)
    b.interfaces.append(iface_b)


def _build_topology(num_nodes: int, num_gs: int = 4, seed: int = 42):
    """Build a mock topology over a connected weighted graph.

    Args:
        num_nodes: Total number of nodes (ground stations + satellites).
        num_gs: Number of ground stations to create.
        seed: Random seed for reproducibility.

    Returns:
        Tuple of (topology_mock, graph, gs_list, sat_list).
    """
    rng = random.Random(seed)
    k = min(4, num_nodes - 1) if num_nodes > 1 else 0
    graph = nx.connected_watts_strogatz_graph(
        num_nodes, k=k, p=0.1, seed=seed
    )
    for u, v in graph.edges():
        graph[u][v]["weight"] = rng.uniform(1.0, 10.0)

    nodes_by_id: dict[int, "Node"] = {}
    sats: list[Satellite] = []
    gs_list: list[GroundStation] = []

    for i in range(num_nodes):
        if i < num_gs:
            node = GroundStation(f"Gnd{i}")
            node.loopback.ipv4 = _loopback_ip(i)
            node.id = i
            gs_list.append(node)
        else:
            node = Satellite(f"Sat{i}")
            node.loopback.ipv4 = _loopback_ip(i)
            node.id = i
            # Mark roughly 20% of satellites as addressable.
            node.set_addressable(i % 5 == 0)
            sats.append(node)
        nodes_by_id[i] = node

    for u, v in graph.edges():
        _link_nodes(nodes_by_id[u], nodes_by_id[v])

    topo = MagicMock()
    topo.status = True
    topo.use_file_routes = False
    topo.project_name = "benchmark"
    topo.current_time_step = 0
    topo.get_satellites.return_value = sats
    topo.get_ground_stations.return_value = gs_list
    topo.satellites = {sat.id: sat for sat in sats}
    topo.ground_stations = {gs.id: gs for gs in gs_list}
    topo.get_current_graph.return_value = graph

    return topo, graph, gs_list, sats


def _routing_tables(nodes: list["Node"]) -> dict[int, list[tuple]]:
    """Return a comparable representation of routing tables."""
    tables: dict[int, list[tuple]] = {}
    for node in nodes:
        tables[node.id] = sorted(
            (r.destination, r.target_node, r.gateway, r.interface.name)
            for r in node.ipv4_routing_table
        )
    return tables


def _clear_routing_tables(nodes: list["Node"]) -> None:
    """Clear current routing tables on a list of nodes."""
    for node in nodes:
        node.ipv4_routing_table.clear()


@pytest.mark.parametrize("num_nodes", [50, 100, 200, 500, 1000, 2000])
def test_benchmark_dijkstra_networkx_vs_igraph(num_nodes: int, capsys) -> None:
    """Benchmark networkx and igraph Dijkstra and assert identical routes."""
    topo, graph, gs_list, sats = _build_topology(num_nodes)
    daemon = StaticRoutingDaemon(topo)
    all_nodes = gs_list + sats
    gs_ifaces, sat_ifaces = daemon._prebuild_iface_maps()

    # Run networkx version once to capture its routing tables.
    _clear_routing_tables(all_nodes)
    daemon._populate_dijkstra(graph, gs_list, sats, gs_ifaces, sat_ifaces)
    nx_tables = _routing_tables(all_nodes)

    # Run igraph version once and compare for correctness.
    _clear_routing_tables(all_nodes)
    daemon._populate_dijkstra_igraph(graph, gs_list, sats, gs_ifaces, sat_ifaces)
    ig_tables = _routing_tables(all_nodes)

    assert nx_tables == ig_tables, (
        f"igraph version produced different routes for {num_nodes} nodes"
    )

    # Benchmark both methods. Scale repetitions so the suite stays fast.
    repeats = max(1, 500 // num_nodes)

    nx_times: list[float] = []
    for _ in range(repeats):
        _clear_routing_tables(all_nodes)
        start = time.perf_counter()
        daemon._populate_dijkstra(graph, gs_list, sats, gs_ifaces, sat_ifaces)
        nx_times.append(time.perf_counter() - start)

    ig_times: list[float] = []
    for _ in range(repeats):
        _clear_routing_tables(all_nodes)
        start = time.perf_counter()
        daemon._populate_dijkstra_igraph(
            graph, gs_list, sats, gs_ifaces, sat_ifaces
        )
        ig_times.append(time.perf_counter() - start)

    nx_avg_ms = (sum(nx_times) / len(nx_times)) * 1000
    ig_avg_ms = (sum(ig_times) / len(ig_times)) * 1000
    speedup = nx_avg_ms / ig_avg_ms if ig_avg_ms > 0 else float("inf")

    with capsys.disabled():
        print(
            f"\nBenchmark {num_nodes} nodes ({repeats} repeats): "
            f"networkx={nx_avg_ms:.3f}ms, "
            f"igraph={ig_avg_ms:.3f}ms, "
            f"speedup={speedup:.2f}x"
        )
