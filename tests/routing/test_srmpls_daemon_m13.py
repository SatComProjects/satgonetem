"""Regression tests for node-scoped SR-MPLS removal (audit M13).

The base daemon contract says ``remove(node)`` removes routes from that node
only.  ``SRMPLSDaemon.remove(node)`` flushed the node but then reset the label
manager, the LFIB, the graph snapshot, and every per-node cache.  The daemon
became uninitialized and the remaining nodes stopped receiving updates.

Only ``remove()`` with no node may tear the daemon down.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import networkx as nx
import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.srmpls_label_manager import SegmentRoutingManager

pytestmark = pytest.mark.unit


class _FakeTopology:
    def __init__(self, ground_stations, graph) -> None:
        self.ground_stations = {node.id: node for node in ground_stations}
        self.satellites = {}
        self.status = True
        self.gonetem_is_on = True
        self._graph = graph

    def get_ground_stations(self):
        return list(self.ground_stations.values())

    def get_satellites(self):
        return []

    def get_current_graph(self):
        return self._graph

    def _get_interface_to_peer(self, node, peer_id):
        return None


def _make_ground_station(node_id: int, loopback: str) -> GroundStation:
    gs = GroundStation(f"Gnd{node_id}")
    gs.loopback.set_ip(loopback)
    gs.container = object()
    gs.execute_command = MagicMock(return_value="")
    return gs


def _make_daemon() -> tuple[SRMPLSDaemon, GroundStation, GroundStation, nx.Graph]:
    removed = _make_ground_station(0, "10.0.0.1")
    remaining = _make_ground_station(1, "10.0.0.2")
    graph = nx.Graph()
    graph.add_node(removed.id)
    graph.add_node(remaining.id)
    topology = _FakeTopology([removed, remaining], graph)

    daemon = SRMPLSDaemon(topology)
    manager = SegmentRoutingManager(node_sid_base=16000)
    for node in (removed, remaining):
        manager.allocate_node_sid(node.id, node.name)
    daemon._label_manager = manager
    daemon.mpls_config = SimpleNamespace(use_php=False)
    daemon._sr_graph = graph
    daemon._enable_mpls_on_all_nodes = lambda max_workers=32: 0
    daemon._enable_mpls = lambda node: None

    daemon.sr_lfib = {removed.id: [object()], remaining.id: [object()]}
    for cache in (
        daemon._prev_sr_gs_routes,
        daemon._prev_sr_sat_routes,
        daemon._prev_sr_node_sid_routes,
    ):
        cache[removed.id] = [f"ip -f mpls route replace 1600{removed.id} dev lo"]
        cache[remaining.id] = [f"ip -f mpls route replace 1600{remaining.id} dev lo"]
    return daemon, removed, remaining, graph


def test_remove_one_node_keeps_the_daemon_initialized_and_other_nodes() -> None:
    daemon, removed, remaining, graph = _make_daemon()

    daemon.remove(removed)

    assert daemon.is_initialized
    assert daemon._label_manager is not None
    assert daemon._sr_graph is graph

    assert removed.id not in daemon.sr_lfib
    assert remaining.id in daemon.sr_lfib
    for cache in (
        daemon._prev_sr_gs_routes,
        daemon._prev_sr_sat_routes,
        daemon._prev_sr_node_sid_routes,
    ):
        assert removed.id not in cache
        assert remaining.id in cache, "other nodes' caches must survive"


def test_remaining_nodes_still_receive_updates_after_a_scoped_remove() -> None:
    daemon, removed, remaining, _graph = _make_daemon()
    del daemon.topology.ground_stations[removed.id]

    daemon.remove(removed)
    # Make sure the assertion cannot be satisfied by the cache seeded by the
    # fixture: a scoped remove must not stop the remaining node from being
    # rebuilt and re-installed on the next update.
    daemon._prev_sr_gs_routes.pop(remaining.id, None)
    daemon.update([])

    assert daemon.is_initialized
    assert daemon.sr_lfib[remaining.id] == []
    assert daemon._prev_sr_gs_routes[remaining.id] == [
        "ip -f mpls route replace 16001 dev lo"
    ], "the remaining node must still have its routes rebuilt and installed"


def test_remove_without_a_node_still_tears_everything_down() -> None:
    daemon, removed, remaining, graph = _make_daemon()

    daemon.remove()

    assert not daemon.is_initialized
    assert daemon._label_manager is None
    assert daemon._sr_graph is None
    assert daemon.sr_lfib == {}
    assert daemon._prev_sr_gs_routes == {}
    assert daemon._prev_sr_sat_routes == {}
    assert daemon._prev_sr_node_sid_routes == {}
