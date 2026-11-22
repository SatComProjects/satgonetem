"""Regression tests for M06: routing manager lifecycle and path robustness.

The routing manager must honour its documented ``-1.0`` failure contract even
when daemon construction raises, always clear state when ``remove()`` fails,
validate endpoints without rejecting subclasses, and serialize lifecycle calls
with the simulation lock.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import networkx as nx
import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.satellite import Satellite
from satgonetem.services.mixins.routing_mgr import RoutingManagerMixin
from satgonetem.services.mixins.simulation_loop import ensure_simulation_lock
from tests.support import make_ground_station, make_satellite


class RecordingDaemon:
    """Minimal daemon that records lifecycle calls and lock ownership."""

    def __init__(
        self,
        topology,
        *,
        init_result=True,
        init_error=None,
        remove_error=None,
    ):
        self.topology = topology
        self.init_result = init_result
        self.init_error = init_error
        self.remove_error = remove_error
        self.init_lock_owned = None
        self.remove_lock_owned = None
        self.remove_calls = []

    def init(self, max_workers=4):
        self.init_lock_owned = ensure_simulation_lock(self.topology)._is_owned()
        if self.init_error is not None:
            raise self.init_error
        return self.init_result

    def remove(self, node=None, max_workers=4):
        self.remove_lock_owned = ensure_simulation_lock(self.topology)._is_owned()
        self.remove_calls.append((node, max_workers))
        if self.remove_error is not None:
            raise self.remove_error

    def update(self, new_links, max_workers=4):
        pass


class DaemonFactory:
    def __init__(self, **daemon_kwargs):
        self.daemon_kwargs = daemon_kwargs
        self.instances = []

    def __call__(self, topology):
        daemon = RecordingDaemon(topology, **self.daemon_kwargs)
        self.instances.append(daemon)
        return daemon


class DummyRoutingTopology(RoutingManagerMixin):
    def __init__(self, routing="static"):
        self.routing = routing
        self.routing_daemon = None
        self.routing_initiated = False
        self._daemon_registry = {}

    def get_routing_initiated(self):
        return self.routing_initiated

    def set_routing_initiated(self, value):
        self.routing_initiated = value


class PathTopology(RoutingManagerMixin):
    def __init__(self, graph):
        self.graph = graph
        self.satellites = {}
        self.ground_stations = {}

    def get_current_graph(self):
        return self.graph


def test_init_routing_returns_minus_one_when_factory_raises(caplog):
    topology = DummyRoutingTopology()
    old_daemon = MagicMock()
    topology.routing_daemon = old_daemon
    topology.routing_initiated = True

    def broken_factory(_topology):
        raise RuntimeError("factory boom")

    topology._daemon_registry["broken"] = broken_factory

    with caplog.at_level(logging.ERROR):
        elapsed = topology.init_routing("broken", max_workers=2)

    assert elapsed == -1.0
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
    old_daemon.remove.assert_called_once_with(max_workers=2)
    assert "factory boom" in caplog.text


class BrokenConstructorDaemon:
    def __init__(self, _topology):
        raise RuntimeError("constructor boom")


def test_init_routing_returns_minus_one_when_constructor_raises(caplog):
    topology = DummyRoutingTopology()
    topology._daemon_registry["broken"] = BrokenConstructorDaemon

    with caplog.at_level(logging.ERROR):
        elapsed = topology.init_routing("broken")

    assert elapsed == -1.0
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
    assert "constructor boom" in caplog.text


@pytest.mark.parametrize(
    "init_result,init_error", [(False, None), (True, RuntimeError)]
)
def test_init_failure_does_not_leave_a_half_installed_daemon(init_result, init_error):
    topology = DummyRoutingTopology()
    topology._daemon_registry["static"] = DaemonFactory(
        init_result=init_result, init_error=init_error
    )

    assert topology.init_routing() == -1.0
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False


def test_init_routing_holds_the_simulation_lock():
    topology = DummyRoutingTopology()
    factory = DaemonFactory()
    topology._daemon_registry["static"] = factory

    topology.init_routing()

    assert factory.instances[0].init_lock_owned is True


def test_delete_routing_holds_the_simulation_lock():
    topology = DummyRoutingTopology()
    factory = DaemonFactory()
    topology._daemon_registry["static"] = factory
    topology.init_routing()

    topology.delete_routing()

    assert factory.instances[0].remove_lock_owned is True


def test_delete_routing_clears_state_when_remove_raises():
    topology = DummyRoutingTopology()
    factory = DaemonFactory(remove_error=RuntimeError("remove boom"))
    topology._daemon_registry["static"] = factory
    topology.init_routing()

    with pytest.raises(RuntimeError, match="remove boom"):
        topology.delete_routing()

    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
    assert topology.routing_method is None


def _path_topology(*nodes, edges=()):
    topology = PathTopology(nx.Graph())
    for node in nodes:
        if isinstance(node, Satellite):
            topology.satellites[node.id] = node
        else:
            topology.ground_stations[node.id] = node
    for source_id, target_id in edges:
        topology.graph.add_edge(source_id, target_id, weight=1.0)
    return topology


def test_get_path_returns_empty_when_target_is_missing_from_graph():
    satellite = make_satellite("Sat0")
    station = make_ground_station("Gnd5")
    topology = _path_topology(satellite, station)
    topology.graph.add_node(satellite.id)

    assert topology.get_path_between_nodes(satellite, station) == []


def test_get_path_returns_empty_for_disconnected_nodes():
    satellite = make_satellite("Sat0")
    station = make_ground_station("Gnd5")
    topology = _path_topology(satellite, station)
    topology.graph.add_node(satellite.id)
    topology.graph.add_node(station.id)

    assert topology.get_path_between_nodes(satellite, station) == []


def test_get_path_accepts_node_subclasses():
    class CustomSatellite(Satellite):
        pass

    satellite = CustomSatellite("Sat0")
    station = make_ground_station("Gnd5")
    topology = _path_topology(satellite, station, edges=[(satellite.id, station.id)])

    assert topology.get_path_between_nodes(satellite, station) == [
        satellite,
        station,
    ]


def test_get_path_rejects_a_non_node_target():
    satellite = make_satellite("Sat0")
    topology = _path_topology(satellite)

    with pytest.raises(TypeError, match="Target"):
        topology.get_path_between_nodes(satellite, SimpleNamespace(id=5))


def test_get_path_rejects_a_non_node_source():
    satellite = make_satellite("Sat0")
    topology = _path_topology(satellite)

    with pytest.raises(TypeError, match="Source"):
        topology.get_path_between_nodes(SimpleNamespace(id=0), satellite)


def test_ground_station_subclass_is_accepted():
    class CustomStation(GroundStation):
        pass

    satellite = make_satellite("Sat0")
    station = CustomStation("Gnd5")
    topology = _path_topology(satellite, station, edges=[(satellite.id, station.id)])

    assert topology.get_path_between_nodes(satellite, station) == [
        satellite,
        station,
    ]
