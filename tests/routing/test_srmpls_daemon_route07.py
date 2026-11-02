"""Regression tests for ROUTE-07: per-source SR rebuild must keep satellites.

``set_sr_custom_path()`` and ``clear_sr_custom_path()`` rebuild only the
affected source via ``_update_sr_route_for_source()``. That method used to
iterate over ground stations only, dropping every route from the source to an
addressable satellite that ``_build_sr_gs_routes()`` had installed. The rebuild
must use the same destination set and skip non-addressable satellites.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import networkx as nx

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


class _FakeLabelManager:
    """Minimal label manager returning deterministic label stacks."""

    def get_custom_path(self, source_id, dest_id):
        return None

    def get_node_sid(self, node_id):
        return 16000 + node_id

    def get_label_stack_for_path(self, path, use_php=False):
        return [16000 + node_id for node_id in path]


class _FakeTopology:
    """Topology stub exposing only what the per-source rebuild needs."""

    def __init__(self, ground_stations, satellites, graph):
        self.ground_stations = {gs.id: gs for gs in ground_stations}
        self.satellites = {sat.id: sat for sat in satellites}
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


def _make_gs(name, loopback_ip, container=True):
    gs = GroundStation(name)
    if container:
        gs.container = object()
    gs.loopback.set_ip(loopback_ip)
    gs.execute_command = MagicMock(return_value="")
    return gs


def _make_sat(name, loopback_ip, addressable):
    sat = Satellite(name)
    sat.set_addressable(addressable)
    sat.loopback.set_ip(loopback_ip)
    return sat


def _make_daemon(topology):
    daemon = SRMPLSDaemon.__new__(SRMPLSDaemon)
    daemon._label_manager = _FakeLabelManager()
    daemon.topology = topology
    daemon.mpls_config = SimpleNamespace(use_php=False)
    daemon.sr_lfib = {}
    daemon._prev_sr_gs_routes = {}
    daemon._prev_sr_sat_routes = {}
    daemon._prev_sr_node_sid_routes = {}
    daemon._enable_mpls = MagicMock()
    return daemon


def _build_topology():
    gs_src = _make_gs("Gnd0", "10.0.0.1")
    sat_dst = _make_sat("Sat1", "10.0.0.2", addressable=True)
    gs_dst = _make_gs("Gnd2", "10.0.0.3", container=False)
    sat_skipped = _make_sat("Sat3", "10.0.0.4", addressable=False)

    # GS source -> Sat1 -> GS2. Sat3 must never become a destination.
    gs_iface = Interface("Gnd0.1")
    sat_iface = Interface("Sat1.0")
    gs_iface.peer = sat_iface
    sat_iface.peer = gs_iface
    gs_iface.set_ip("10.10.0.1")
    sat_iface.set_ip("10.10.0.2")
    gs_src.interfaces = [gs_iface]
    sat_dst.interfaces = [sat_iface]

    graph = nx.Graph()
    graph.add_edge(0, 1, weight=1)
    graph.add_edge(1, 2, weight=1)
    graph.add_node(3)

    topology = _FakeTopology(
        ground_stations=[gs_src, gs_dst],
        satellites=[sat_dst, sat_skipped],
        graph=graph,
    )
    return topology, gs_src, sat_dst


def test_update_route_for_source_keeps_addressable_satellite_destination():
    """Satellite routes survive the per-source rebuild triggered by custom paths."""
    topology, gs_src, sat_dst = _build_topology()
    daemon = _make_daemon(topology)

    daemon._update_sr_route_for_source(gs_src.id)

    entries = daemon.sr_lfib[gs_src.id]
    destinations = {entry.destination for entry in entries}
    assert "10.0.0.2" in destinations, (
        "per-source rebuild dropped the addressable satellite destination"
    )
    assert "10.0.0.3" in destinations, "ground-station destination must remain"
    assert "10.0.0.4" not in destinations, "non-addressable satellite must be skipped"

    sat_entry = next(e for e in entries if e.destination == "10.0.0.2")
    assert sat_entry.label_stack == [16001]
    assert sat_entry.next_hop == "10.10.0.2"
    assert sat_entry.interface is gs_src.interfaces[0]
    assert sat_entry.fec_prefix == 32

    daemon._enable_mpls.assert_called_once_with(gs_src)
    gs_src.execute_command.assert_called_once()
