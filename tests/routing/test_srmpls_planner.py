"""Direct tests for the SR-MPLS route planner collaborators."""

from __future__ import annotations

import networkx as nx
import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.srmpls_label_manager import SegmentRoutingManager
from satgonetem.routing.srmpls_planner import SRMPLSRoutePlanner

pytestmark = pytest.mark.unit


class _FakeTopology:
    def __init__(self, ground_stations, satellites) -> None:
        self.ground_stations = {node.id: node for node in ground_stations}
        self.satellites = {node.id: node for node in satellites}

    def get_ground_stations(self):
        return list(self.ground_stations.values())

    def get_satellites(self):
        return list(self.satellites.values())

    def _get_interface_to_peer(self, node, peer_id):
        for iface in node.interfaces:
            try:
                if int(iface.name.split(".")[1]) == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None


def _make_ground_station(node_id: int, loopback: str) -> GroundStation:
    gs = GroundStation(f"Gnd{node_id}")
    gs.loopback.set_ip(loopback)
    return gs


def _make_satellite(node_id: int, loopback: str, addressable: bool = True) -> Satellite:
    sat = Satellite(f"Sat{node_id}")
    sat.set_addressable(addressable)
    sat.loopback.set_ip(loopback)
    return sat


def _connect(left, right, left_ip: str, right_ip: str) -> Interface:
    left_iface = Interface(f"{left.name}.{right.id}")
    right_iface = Interface(f"{right.name}.{left.id}")
    left_iface.peer = right_iface
    right_iface.peer = left_iface
    left_iface.set_ip(left_ip)
    right_iface.set_ip(right_ip)
    left.interfaces.append(left_iface)
    right.interfaces.append(right_iface)
    return left_iface


def _manager(*nodes) -> SegmentRoutingManager:
    manager = SegmentRoutingManager(node_sid_base=16000)
    for node in nodes:
        manager.allocate_node_sid(node.id, node.name)
    return manager


def _chain():
    gnd0 = _make_ground_station(0, "10.0.0.1")
    sat1 = _make_satellite(1, "10.0.0.2")
    gnd2 = _make_ground_station(2, "10.0.0.3")
    _connect(gnd0, sat1, "10.10.0.1", "10.10.0.2")
    _connect(sat1, gnd2, "10.10.0.3", "10.10.0.4")

    graph = nx.Graph()
    graph.add_edge(gnd0.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, gnd2.id, weight=1)
    topology = _FakeTopology([gnd0, gnd2], [sat1])
    return topology, graph, gnd0, sat1, gnd2


def test_build_all_makes_every_source_and_destination_a_pair() -> None:
    topology, graph, gnd0, sat1, gnd2 = _chain()
    manager = _manager(gnd0, sat1, gnd2)

    lfib = SRMPLSRoutePlanner(topology).build_all(graph, manager, use_php=False)

    assert set(lfib) == {gnd0.id, sat1.id, gnd2.id}
    assert {entry.destination for entry in lfib[gnd0.id]} == {
        sat1.loopback.ipv4,
        gnd2.loopback.ipv4,
    }
    entry = next(
        entry for entry in lfib[gnd0.id] if entry.destination == gnd2.loopback.ipv4
    )
    assert entry.label_stack == [16001, 16002]
    assert entry.next_hop == "10.10.0.2"
    assert entry.fec_prefix == 32


def test_build_all_prefers_a_custom_path_over_the_shortest_path() -> None:
    gnd0 = _make_ground_station(0, "10.0.0.1")
    gnd2 = _make_ground_station(2, "10.0.0.3")
    sat1 = _make_satellite(1, "10.0.0.2")
    sat3 = _make_satellite(3, "10.0.0.4", addressable=False)
    _connect(gnd0, sat1, "10.10.0.1", "10.10.0.2")
    _connect(sat1, gnd2, "10.10.0.3", "10.10.0.4")
    _connect(gnd0, sat3, "10.20.0.1", "10.20.0.2")
    _connect(sat3, gnd2, "10.20.0.3", "10.20.0.4")

    graph = nx.Graph()
    graph.add_edge(gnd0.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, gnd2.id, weight=1)
    graph.add_edge(gnd0.id, sat3.id, weight=10)
    graph.add_edge(sat3.id, gnd2.id, weight=10)
    topology = _FakeTopology([gnd0, gnd2], [sat1, sat3])
    manager = _manager(gnd0, gnd2, sat1, sat3)
    manager.set_custom_path(gnd0.id, gnd2.id, [gnd0.id, sat3.id, gnd2.id])

    lfib = SRMPLSRoutePlanner(topology).build_all(graph, manager, use_php=False)

    entry = next(
        entry for entry in lfib[gnd0.id] if entry.destination == gnd2.loopback.ipv4
    )
    assert entry.next_hop == "10.20.0.2"
    assert entry.label_stack == [16003, 16002]


def test_build_all_honours_php() -> None:
    topology, graph, gnd0, _sat1, gnd2 = _chain()
    manager = _manager(
        *topology.ground_stations.values(), *topology.satellites.values()
    )

    lfib = SRMPLSRoutePlanner(topology).build_all(graph, manager, use_php=True)

    entry = next(
        entry for entry in lfib[gnd0.id] if entry.destination == gnd2.loopback.ipv4
    )
    assert entry.label_stack == [16001]


def test_build_for_source_uses_single_source_shortest_path() -> None:
    topology, graph, gnd0, sat1, gnd2 = _chain()
    manager = _manager(gnd0, sat1, gnd2)

    entries = SRMPLSRoutePlanner(topology).build_for_source(
        gnd0, graph, manager, use_php=False
    )

    assert {entry.destination for entry in entries} == {
        sat1.loopback.ipv4,
        gnd2.loopback.ipv4,
    }
    assert entries[0].interface is gnd0.interfaces[0]


def test_non_addressable_satellite_is_neither_source_nor_destination() -> None:
    topology, graph, gnd0, _sat1, gnd2 = _chain()
    hidden = _make_satellite(3, "10.0.0.4", addressable=False)
    topology.satellites[hidden.id] = hidden
    graph.add_node(hidden.id)
    manager = _manager(
        *topology.ground_stations.values(), *topology.satellites.values()
    )

    lfib = SRMPLSRoutePlanner(topology).build_all(graph, manager, use_php=False)

    assert hidden.id not in lfib
    assert all(
        entry.destination != hidden.loopback.ipv4
        for entries in lfib.values()
        for entry in entries
    )
