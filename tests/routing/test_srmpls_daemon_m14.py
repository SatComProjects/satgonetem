"""Regression tests for stale per-source SR-MPLS routes (audit M14).

``_update_sr_route_for_source`` replaced the cached command list but never
emitted deletes for routes that disappeared from the rebuild, unlike the
full ground-station pass which diffs against its cache.  When a destination
became unreachable, its old ``ip route replace`` line stayed in the kernel.

The rebuild must prepend deletes for stale route identities in the same
fail-fast batch that installs the replacement commands.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
        self.gonetem_is_on = True
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
    gs.container = object()
    gs.execute_command = MagicMock(return_value="")
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


def _make_topology() -> tuple[_FakeTopology, GroundStation, GroundStation, nx.Graph]:
    source = _make_ground_station(0, "10.0.0.1")
    far_gs = _make_ground_station(2, "10.0.0.3")
    sat1 = _make_satellite(1, "10.0.0.2")
    sat3 = _make_satellite(3, "10.0.0.4")
    sat3.set_addressable(False)
    _connect(source, sat1, "10.10.0.1", "10.10.0.2")
    _connect(sat1, far_gs, "10.10.0.3", "10.10.0.4")
    _connect(source, sat3, "10.10.0.5", "10.10.0.6")
    _connect(sat3, far_gs, "10.10.0.7", "10.10.0.8")

    graph = nx.Graph()
    graph.add_edge(source.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, far_gs.id, weight=1)
    graph.add_edge(source.id, sat3.id, weight=1)
    graph.add_edge(sat3.id, far_gs.id, weight=1)

    topology = _FakeTopology([source, far_gs], [sat1, sat3], graph)
    return topology, source, far_gs, graph


def _make_daemon(topology: _FakeTopology) -> SRMPLSDaemon:
    daemon = SRMPLSDaemon(topology)
    manager = SegmentRoutingManager(node_sid_base=16000)
    for node in [
        *topology.satellites.values(),
        *topology.ground_stations.values(),
    ]:
        manager.allocate_node_sid(node.id, node.name)
    daemon._label_manager = manager
    daemon.mpls_config = SimpleNamespace(use_php=False)
    daemon._enable_mpls = lambda node: None
    return daemon


def _batch_text(source: GroundStation) -> str:
    return source.execute_command.call_args.args[0][2]


def test_unreachable_destination_is_deleted_from_kernel_and_cache() -> None:
    topology, source, far_gs, graph = _make_topology()
    daemon = _make_daemon(topology)

    daemon._update_sr_route_for_source(source.id)
    first_batch = _batch_text(source)
    assert f"ip route replace {far_gs.loopback.ipv4}/32" in first_batch
    assert any(
        far_gs.loopback.ipv4 in line for line in daemon._prev_sr_gs_routes[source.id]
    )

    # The transit links to the far ground station disappear.
    graph.remove_edge(1, far_gs.id)
    graph.remove_edge(3, far_gs.id)
    source.execute_command.reset_mock()

    daemon._update_sr_route_for_source(source.id)

    second_batch = _batch_text(source)
    assert second_batch.startswith("set -e; ")
    assert f"ip route del {far_gs.loopback.ipv4}/32" in second_batch
    assert not any(
        far_gs.loopback.ipv4 in line for line in daemon._prev_sr_gs_routes[source.id]
    )


def test_changed_path_replaces_the_route_without_deleting_it() -> None:
    topology, source, far_gs, _graph = _make_topology()
    daemon = _make_daemon(topology)
    daemon._update_sr_route_for_source(source.id)
    source.execute_command.reset_mock()

    daemon._label_manager.set_custom_path(
        source.id, far_gs.id, [source.id, 3, far_gs.id]
    )
    daemon._update_sr_route_for_source(source.id)

    batch = _batch_text(source)
    assert f"ip route del {far_gs.loopback.ipv4}/32" not in batch
    assert f"ip route replace {far_gs.loopback.ipv4}/32" in batch
