"""Regression tests for SR-MPLS label-stack semantics (audit M11).

The daemon reported a stack for the complete path (source included) through
the public API, but installed a stack built from ``path[1:]``.  The second
form silently dropped the first transit node's SID: for a path
``[Gnd0, Sat1, Sat2]`` the API reported ``[16001, 16002]`` (no PHP) while the
installed route only pushed ``16002``.

``docs/models.md`` and ``SegmentRoutingManager.get_label_stack_for_path``
define one stack form: the input is the complete path, the source is not
labelled, and the destination is labelled only when PHP is disabled.  These
tests pin that the route builder uses that form.
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
    """Topology stub exposing the lookups used by SR-MPLS route building."""

    def __init__(self, ground_stations, satellites, graph) -> None:
        self.ground_stations = {node.id: node for node in ground_stations}
        self.satellites = {node.id: node for node in satellites}
        self.status = False
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


def _make_satellite(node_id: int, loopback: str) -> Satellite:
    sat = Satellite(f"Sat{node_id}")
    sat.set_addressable(True)
    sat.loopback.set_ip(loopback)
    return sat


def _make_ground_station(node_id: int, loopback: str) -> GroundStation:
    gs = GroundStation(f"Gnd{node_id}")
    gs.loopback.set_ip(loopback)
    gs.container = object()
    gs.execute_command = lambda command: ""
    return gs


def _connect(left, right, left_ip: str, right_ip: str) -> None:
    left_iface = Interface(f"{left.name}.{right.id}")
    right_iface = Interface(f"{right.name}.{left.id}")
    left_iface.peer = right_iface
    right_iface.peer = left_iface
    left_iface.set_ip(left_ip)
    right_iface.set_ip(right_ip)
    left.interfaces.append(left_iface)
    right.interfaces.append(right_iface)


def _build_topology() -> tuple[_FakeTopology, GroundStation, Satellite]:
    gnd0 = _make_ground_station(0, "10.0.0.1")
    sat1 = _make_satellite(1, "10.0.0.2")
    sat2 = _make_satellite(2, "10.0.0.3")
    _connect(gnd0, sat1, "10.10.0.1", "10.10.0.2")
    _connect(sat1, sat2, "10.10.0.3", "10.10.0.4")

    graph = nx.Graph()
    graph.add_edge(gnd0.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, sat2.id, weight=1)

    topology = _FakeTopology([gnd0], [sat1, sat2], graph)
    return topology, gnd0, sat2


def _make_daemon(topology: _FakeTopology, use_php: bool) -> SRMPLSDaemon:
    daemon = SRMPLSDaemon(topology)
    manager = SegmentRoutingManager(node_sid_base=16000)
    for node in [
        *topology.satellites.values(),
        *topology.ground_stations.values(),
    ]:
        manager.allocate_node_sid(node.id, node.name)
    daemon._label_manager = manager
    daemon.mpls_config = SimpleNamespace(use_php=use_php)
    return daemon


@pytest.mark.parametrize(
    ("use_php", "expected_stack"),
    [
        (False, [16001, 16002]),
        (True, [16001]),
    ],
)
def test_built_route_pushes_the_documented_full_path_stack(
    use_php: bool, expected_stack: list[int]
) -> None:
    """The installed stack must label the same nodes the API reports."""
    topology, gnd0, sat2 = _build_topology()
    daemon = _make_daemon(topology, use_php=use_php)

    daemon._build_sr_gs_routes()

    entry = next(
        entry
        for entry in daemon.sr_lfib[gnd0.id]
        if entry.destination == sat2.loopback.ipv4
    )
    assert entry.label_stack == expected_stack
    assert entry.label_stack == daemon._label_manager.get_label_stack_for_path(
        [gnd0.id, 1, sat2.id], use_php=use_php
    )


def test_reported_custom_path_stack_matches_installed_route() -> None:
    """set_sr_custom_path() and the route builder must agree on the stack."""
    topology, gnd0, sat2 = _build_topology()
    daemon = _make_daemon(topology, use_php=False)
    path = [gnd0.id, 1, sat2.id]

    reported = daemon.set_sr_custom_path(gnd0.id, sat2.id, path)["label_stack"]

    daemon._build_sr_gs_routes()
    installed = next(
        entry.label_stack
        for entry in daemon.sr_lfib[gnd0.id]
        if entry.destination == sat2.loopback.ipv4
    )

    assert reported == [16001, 16002]
    assert installed == reported
