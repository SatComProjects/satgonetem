"""Slow 3x3 Walker scale smoke: all directed ground-station pings."""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator

import networkx as nx
import pytest

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.traffic import PingConfig
from tests.support.live import (
    LiveEnvironment,
    assert_ping,
    teardown_live_topology,
    unique_project_name,
    wait_for_flow,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_WARMUP = PingConfig(count=1, timeout_sec=2.0, interval_sec=0.2, preload=1)
_MEASURED = PingConfig(count=3, timeout_sec=2.0, interval_sec=0.2, preload=1)


def _build_scale_topology() -> TopologyManager:
    """Build a 3x3 Walker constellation at a fully connected epoch.

    The 2026-11-03 15:00 epoch gives every one of the five default European
    ground stations a path to every other (checked before adding containers).
    """
    from satgonetem.utils.project_builder import create_test_project

    base_name = unique_project_name("LiveScale3x3")
    project = create_test_project(
        simulation_name=base_name,
        start_date="03/11/2026 15:00:00",
        end_date="03/11/2026 15:10:00",
    )
    shell = project.walker_shells[0]
    shell.constellation_property.amount_of_orbit_plane = 3
    shell.constellation_property.amount_of_satellite_per_orbit_plane = 3
    topology = TopologyManager.from_satcom(
        project, NetworkConfig(update_time=1, project_name=base_name)
    )

    graph = topology.get_current_graph()
    ground_stations = sorted(topology.get_ground_stations(), key=lambda node: node.id)
    for source, destination in itertools.permutations(ground_stations, 2):
        if not nx.has_path(graph, source.id, destination.id):
            raise RuntimeError(
                "3x3 scale epoch is not fully connected: "
                f"{source.name} -> {destination.name} has no graph path"
            )
    return topology


@pytest.fixture(scope="module")
def scale_topology(live_env: LiveEnvironment) -> Iterator[TopologyManager]:
    """Start the 3x3 topology, configure it, and always tear it down."""
    topology = _build_scale_topology()
    project_id: str | None = None
    try:
        topology.start_gonetem()
        project_id = topology.project_id
        if not project_id:
            raise RuntimeError("GoNetEm returned an empty project id")
        topology.set_ip_addresses()
        topology.init_routing(routing_method="static")
        if not topology.get_routing_initiated():
            raise RuntimeError("static routing initialisation failed")
        yield topology
    finally:
        if project_id is None:
            project_id = getattr(topology, "project_id", None)
        teardown_live_topology(topology, live_env.docker, project_id)


def _warm_up(topology: TopologyManager, pairs: list[tuple]) -> None:
    """Prime ARP with one throwaway ping per directed pair."""
    flows = [
        topology.ping(source, destination, _WARMUP)[0] for source, destination in pairs
    ]
    deadline = time.monotonic() + 60.0
    for flow in flows:
        wait_for_flow(flow, timeout=max(0.1, deadline - time.monotonic()))
        flow.discard_result()


def test_3x3_all_directed_ground_station_pings(
    scale_topology: TopologyManager,
) -> None:
    """Every directed ground-station pair receives replies after warm-up."""
    topology = scale_topology
    assert len(topology.get_satellites()) == 9
    ground_stations = sorted(topology.get_ground_stations(), key=lambda node: node.id)
    assert len(ground_stations) == 5

    pairs = list(itertools.permutations(ground_stations, 2))
    assert len(pairs) == 20
    _warm_up(topology, pairs)

    flows = [
        (source, destination, topology.ping(source, destination, _MEASURED)[0])
        for source, destination in pairs
    ]
    deadline = time.monotonic() + 120.0
    for source, destination, flow in flows:
        result = assert_ping(
            flow,
            timeout=max(0.1, deadline - time.monotonic()),
            max_rtt_ms=5000.0,
        )
        assert result.packets_received == _MEASURED.count, (
            f"{source.name} -> {destination.name}: "
            f"{result.packets_received}/{_MEASURED.count} replies"
        )
