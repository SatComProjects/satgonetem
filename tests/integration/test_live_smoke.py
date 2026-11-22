"""Non-root live smoke: GoNetEm, Docker, planned addresses, routes, pings."""

from __future__ import annotations

import pytest

from satgonetem.services.topology_satcom import TopologyManager
from satgonetem.traffic import PingConfig
from tests.support.live import (
    LiveEnvironment,
    assert_ping,
    project_containers,
    wait_for_flow,
)

pytestmark = pytest.mark.integration

_WARMUP = PingConfig(count=1, timeout_sec=2.0, interval_sec=0.2, preload=1)
_MEASURED = PingConfig(count=3, timeout_sec=2.0, interval_sec=0.2, preload=1)


def _ordered_nodes(topology: TopologyManager) -> list:
    return sorted(
        [*topology.get_satellites(), *topology.get_ground_stations()],
        key=lambda node: (node.__class__.__name__, node.id),
    )


def test_server_version_and_required_images(live_env: LiveEnvironment) -> None:
    """The gRPC handshake succeeds and every required image is local."""
    assert live_env.server_address
    assert live_env.server_version, "ServerGetVersion returned an empty version"
    for image in live_env.required_images:
        assert live_env.docker.images.get(image) is not None


def test_three_node_topology_is_live(
    live_env: LiveEnvironment, live_topology: TopologyManager
) -> None:
    """Every node has a running container, a PID, and its planned addresses."""
    topology = live_topology
    assert len(topology.get_satellites()) == 2
    assert len(topology.get_ground_stations()) == 1

    project_id = topology.project_id
    assert project_id, "GoNetEm project id is not set"
    nodes = _ordered_nodes(topology)
    assert len(nodes) == 3

    for node in nodes:
        assert node.container is not None, f"{node.name}: no Docker container"
        assert node.container.name == f"ntm{project_id}.{node.name}"
        assert node.container_pid is not None and node.container_pid > 0, (
            f"{node.name}: container PID was not attached"
        )
        assert node.project_id == project_id
        assert node.grpc_client is not None
        node.container.reload()
        assert node.container.status == "running", f"{node.name} is not running"

        addresses = node.execute_command("ip -4 -o addr show")
        expected = [f"{node.loopback.ipv4}/{node.loopback.prefix_len}"]
        expected.extend(
            f"{interface.ipv4}/{interface.prefix_len}"
            for interface in node.interfaces
            if interface.ipv4
        )
        for address in expected:
            assert address in addresses, (
                f"{node.name}: planned address {address} is not configured; "
                f"live addresses:\n{addresses}"
            )

    assert len(project_containers(live_env.docker, project_id)) == 3


def test_links_and_static_routing_are_installed(
    live_topology: TopologyManager,
) -> None:
    """One ISL and one GSL exist and static routes reach every node."""
    topology = live_topology
    link_types = [link.type for link in topology.get_links()]
    assert link_types.count("InterSatelliteLink") == 1, link_types
    assert link_types.count("GroundStationLink") == 1, link_types
    assert topology.get_routing_initiated(), "static routing is not initiated"

    nodes = _ordered_nodes(topology)
    for node in nodes:
        routes = node.execute_command("ip -4 route show")
        assert " via " in routes, (
            f"{node.name}: no static route with a gateway was installed:\n{routes}"
        )

    satellites = sorted(topology.get_satellites(), key=lambda node: node.id)
    ground_station = topology.get_ground_stations()[0]
    resolved = ground_station.execute_command(
        f"ip route get {satellites[1].loopback.ipv4}"
    )
    assert satellites[1].loopback.ipv4 in resolved
    assert " via " in resolved, (
        f"{ground_station.name}: no routed path to {satellites[1].name}:\n{resolved}"
    )


def test_bidirectional_gsl_and_isl_pings(live_topology: TopologyManager) -> None:
    """GSL+ISL pings in both directions and an ISL-only ping lose nothing."""
    topology = live_topology
    satellites = sorted(topology.get_satellites(), key=lambda node: node.id)
    sat0, sat1 = satellites
    ground_station = topology.get_ground_stations()[0]

    cases = (
        ("ground station -> satellite over GSL+ISL", ground_station, sat1),
        ("satellite -> ground station over ISL+GSL", sat1, ground_station),
        ("satellite -> satellite over ISL", sat0, sat1),
    )
    for label, source, destination in cases:
        warmup = topology.ping(source, destination, _WARMUP)[0]
        wait_for_flow(warmup, timeout=30.0)
        # The first packet only primes ARP; its RTT must not be measured.
        warmup.discard_result()

        flow = topology.ping(source, destination, _MEASURED)[0]
        result = assert_ping(flow, timeout=30.0, max_rtt_ms=5000.0)
        assert result.packets_transmitted == _MEASURED.count
        assert result.packets_received == _MEASURED.count, label
