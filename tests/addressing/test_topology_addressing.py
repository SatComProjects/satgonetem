"""Integration tests for the topology-wide IPv4 addressing plan."""

from __future__ import annotations

import ipaddress

import pytest

from satgonetem.addressing import NodeKind
from satgonetem.routing.static_daemon import StaticRoutingDaemon
from satgonetem.services.topology_satcom import TopologyManager


@pytest.fixture(scope="module")
def topology() -> TopologyManager:
    return TopologyManager.from_file("topology_files/test_topology.json")


def test_network_config_roundtrip(tmp_path, topology):
    path = tmp_path / "topology.json"
    topology.to_file(str(path))
    reloaded = TopologyManager.from_file(str(path))

    assert reloaded.ground_station_pool == "10.0.0.0/10"
    assert reloaded.satellite_pool == "10.128.0.0/9"
    assert reloaded.user_terminal_pool == "10.64.0.0/10"
    assert reloaded.ground_station_prefix_len == 20
    assert reloaded.satellite_prefix_len == 27
    assert reloaded.user_terminal_prefix_len == 20
    assert reloaded.address_plan is not None


def test_pool_capacities_are_large_enough(topology):
    plan = topology.address_plan
    assert plan is not None
    assert plan.capacity_for(NodeKind.GROUND_STATION) == 1024
    assert plan.capacity_for(NodeKind.SATELLITE) == 262_144
    assert plan.capacity_for(NodeKind.USER_TERMINAL) == 1024


def test_ground_stations_own_slash20_and_loopback(topology):
    blocks = set()
    for gs in topology.get_ground_stations():
        assert gs.address_block is not None
        assert gs.address_block.prefixlen == 20
        assert gs.address_block.subnet_of(ipaddress.ip_network("10.0.0.0/10"))
        assert gs.loopback.ipv4 == str(gs.address_block.network_address + 1)
        assert gs.loopback.prefix_len == 32
        blocks.add(gs.address_block)
    assert len(blocks) == len(topology.get_ground_stations())


def test_satellites_own_slash27_and_loopback(topology):
    blocks = set()
    for sat in topology.get_satellites():
        assert sat.address_block is not None
        assert sat.address_block.prefixlen == 27
        assert sat.address_block.subnet_of(ipaddress.ip_network("10.128.0.0/9"))
        assert sat.loopback.ipv4 == str(sat.address_block.network_address + 1)
        assert sat.loopback.prefix_len == 32
        blocks.add(sat.address_block)
    assert len(blocks) == len(topology.get_satellites())


def test_every_link_has_a_unique_valid_slash31(topology):
    seen_networks: set[ipaddress.IPv4Network] = set()
    seen_addresses: set[str] = set()

    for link in topology.get_links():
        if1, if2 = link.peer_interfaces
        assert if1.prefix_len == if2.prefix_len == 31
        network = ipaddress.ip_network(
            f"{if1.ipv4}/{if1.prefix_len}", strict=False
        )
        assert network == ipaddress.ip_network(
            f"{if2.ipv4}/{if2.prefix_len}", strict=False
        )
        assert network not in seen_networks
        seen_networks.add(network)
        for iface in (if1, if2):
            assert ipaddress.ip_address(iface.ipv4) in network
            assert iface.ipv4 not in seen_addresses
            seen_addresses.add(iface.ipv4)


def test_all_loopbacks_and_links_are_inside_ten_slash8(topology):
    pool = ipaddress.ip_network("10.0.0.0/8")
    for node in list(topology.get_satellites()) + list(
        topology.get_ground_stations()
    ):
        assert ipaddress.ip_address(node.loopback.ipv4) in pool
        for iface in node.interfaces:
            assert ipaddress.ip_address(iface.ipv4) in pool


def test_ground_station_links_are_allocated_from_the_ground_station_block(topology):
    for link in topology.get_links():
        if link.type != "GroundStationLink":
            continue
        gs_end = (
            link.source if link.source.type == "GroundStation" else link.target
        )
        for iface in link.peer_interfaces:
            assert ipaddress.ip_network(
                f"{iface.ipv4}/{iface.prefix_len}", strict=False
            ).subnet_of(gs_end.address_block)


def test_static_routes_target_loopbacks_with_slash32(topology):
    daemon = StaticRoutingDaemon(topology)
    gs_ifaces, sat_ifaces = daemon._prebuild_iface_maps()
    daemon._populate_dijkstra(
        topology.get_current_graph(),
        topology.get_ground_stations(),
        topology.get_satellites(),
        gs_ifaces,
        sat_ifaces,
    )

    loopback_to_node = {
        node.loopback.ipv4: node
        for node in list(topology.get_ground_stations())
        + list(topology.get_satellites())
    }
    entries = [
        entry
        for node in list(topology.get_ground_stations())
        + list(topology.get_satellites())
        for entry in node.ipv4_routing_table
    ]
    assert entries
    for entry in entries:
        assert entry.prefix == 32
        assert entry.destination in loopback_to_node
        assert entry.target_node == loopback_to_node[entry.destination].name
