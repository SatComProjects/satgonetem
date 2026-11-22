"""Regression tests for the SR-MPLS satellite LFIB contract (audit M15).

The class docstring claimed the satellite LFIB is O(degree) and installed
once, but addressable satellites are also ingress sources: they receive a
label-stack route for every destination on every rebuild, so their table is
O(N). ``get_sr_statistics`` reported every LFIB source as
``ground_stations_with_routes``, counting satellites as ground stations.

These tests pin the implemented contract and the corrected statistics
without changing route installation.
"""

from __future__ import annotations

from types import SimpleNamespace

import networkx as nx
import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.srmpls_label_manager import SegmentRoutingManager

pytestmark = pytest.mark.unit


class _FakeTopology:
    def __init__(self, ground_stations, satellites, graph) -> None:
        self.ground_stations = {node.id: node for node in ground_stations}
        self.satellites = {node.id: node for node in satellites}
        self.status = True
        self.gonetem_is_on = False
        self._graph = graph

    def get_ground_stations(self):
        return list(self.ground_stations.values())

    def get_satellites(self):
        return list(self.satellites.values())

    def get_current_graph(self):
        return self._graph

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


def _make_satellite(node_id: int, loopback: str) -> Satellite:
    sat = Satellite(f"Sat{node_id}")
    sat.set_addressable(True)
    sat.loopback.set_ip(loopback)
    return sat


def _connect(left, right, left_ip: str, right_ip: str) -> None:
    left_iface = Interface(f"{left.name}.{right.id}")
    right_iface = Interface(f"{right.name}.{left.id}")
    left_iface.peer = right_iface
    right_iface.peer = left_iface
    left_iface.set_ip(left_ip)
    right_iface.set_ip(right_ip)
    left.interfaces.append(left_iface)
    right.interfaces.append(right_iface)


def _make_daemon() -> tuple[SRMPLSDaemon, GroundStation, Satellite]:
    gnd0 = _make_ground_station(0, "10.0.0.1")
    sat1 = _make_satellite(1, "10.0.0.2")
    gnd2 = _make_ground_station(2, "10.0.0.3")
    _connect(gnd0, sat1, "10.10.0.1", "10.10.0.2")
    _connect(sat1, gnd2, "10.10.0.3", "10.10.0.4")

    graph = nx.Graph()
    graph.add_edge(gnd0.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, gnd2.id, weight=1)
    topology = _FakeTopology([gnd0, gnd2], [sat1], graph)

    daemon = SRMPLSDaemon(topology)
    manager = SegmentRoutingManager(node_sid_base=16000)
    for node in (gnd0, sat1, gnd2):
        manager.allocate_node_sid(node.id, node.name)
    daemon._label_manager = manager
    daemon.mpls_config = SimpleNamespace(use_php=False)
    return daemon, gnd0, sat1


def test_addressable_satellite_is_an_ingress_source() -> None:
    daemon, gnd0, sat1 = _make_daemon()

    daemon._build_sr_gs_routes()

    assert gnd0.id in daemon.sr_lfib
    assert sat1.id in daemon.sr_lfib, (
        "addressable satellites receive ingress label-stack routes"
    )
    destinations = {entry.destination for entry in daemon.sr_lfib[sat1.id]}
    assert "10.0.0.1" in destinations
    assert "10.0.0.3" in destinations


def test_statistics_count_ground_stations_separately_from_all_sources() -> None:
    daemon, gnd0, sat1 = _make_daemon()
    daemon._build_sr_gs_routes()

    stats = daemon.get_sr_statistics()

    assert stats["nodes_with_routes"] == 3
    assert stats["ground_stations_with_routes"] == 2
    assert stats["sr_routes"] == sum(len(v) for v in daemon.sr_lfib.values())
