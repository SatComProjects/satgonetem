"""Tests for StaticRoutingDaemon."""

import pytest
import networkx as nx
from unittest.mock import MagicMock, patch

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.models.routing_entry import RoutingEntry
from satgonetem.routing.static_daemon import StaticRoutingDaemon


def make_entry(dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16):
    """Build a minimal RoutingEntry backed by a real Interface."""
    iface = Interface(name=iface_name)
    return RoutingEntry(destination=dst, interface=iface, gateway=gw, prefix=prefix)


def make_topology(status=True, satellites=None, ground_stations=None):
    """Build a minimal topology mock with sane defaults for the current daemon API.

    Sets use_file_routes=False so _populate_routing_tables takes the Dijkstra
    branch. satellites and ground_stations are empty dicts so the apply/prebuild
    helpers are no-ops without real node data.
    """
    topo = MagicMock()
    topo.status = status
    topo.use_file_routes = False
    topo.get_satellites.return_value = satellites or []
    topo.get_ground_stations.return_value = ground_stations or []
    topo.satellites = {}
    topo.ground_stations = {}
    topo.get_current_graph.return_value = nx.Graph()
    return topo


@pytest.fixture
def topology():
    return make_topology()


@pytest.fixture
def daemon(topology):
    return StaticRoutingDaemon(topology)


class TestStaticRoutingDaemonInit:
    """Tests for StaticRoutingDaemon.init()."""

    def test_returns_true_on_success(self, daemon):
        result = daemon.init()

        assert result is True

    def test_populates_routing_tables(self, daemon):
        with patch.object(daemon, "_populate_routing_tables") as mock_pop:
            daemon.init()

        mock_pop.assert_called_once()

    def test_clears_previous_routing_table_on_each_node(self, daemon, topology):
        node = Node("Sat0")
        node.ipv4_previous_routing_table = [make_entry()]
        topology.get_satellites.return_value = [node]
        topology.get_ground_stations.return_value = []

        with patch.object(daemon, "_populate_routing_tables"):
            daemon.init()

        assert node.ipv4_previous_routing_table == []

    def test_applies_ground_station_routes(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes") as mock_gs, \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        mock_gs.assert_called_once()

    def test_applies_satellite_routes(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes") as mock_sat:
            daemon.init()

        mock_sat.assert_called_once()

    def test_returns_false_on_exception(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes", side_effect=RuntimeError("boom")):
            result = daemon.init()

        assert result is False

    def test_passes_max_workers_to_change_methods(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes") as mock_gs, \
             patch.object(daemon, "_apply_satellite_routes") as mock_sat:
            daemon.init(max_workers=8)

        mock_gs.assert_called_once_with(max_workers=8)
        mock_sat.assert_called_once_with(max_workers=8)


class TestStaticRoutingDaemonUpdate:
    """Tests for StaticRoutingDaemon.update()."""

    def test_populates_routing_tables(self, daemon):
        with patch.object(daemon, "_populate_routing_tables") as mock_pop:
            daemon.update(new_links=[])

        mock_pop.assert_called_once()

    def test_applies_routes_when_topology_active(self, daemon, topology):
        topology.status = True

        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes") as mock_gs, \
             patch.object(daemon, "_apply_satellite_routes") as mock_sat:
            daemon.update(new_links=[])

        mock_gs.assert_called_once()
        mock_sat.assert_called_once()

    def test_skips_route_application_when_topology_inactive(self, daemon, topology):
        topology.status = False

        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes") as mock_gs, \
             patch.object(daemon, "_apply_satellite_routes") as mock_sat:
            daemon.update(new_links=[])

        mock_gs.assert_not_called()
        mock_sat.assert_not_called()

    def test_logs_warning_when_topology_inactive(self, daemon, topology, caplog):
        import logging

        topology.status = False

        with patch.object(daemon, "_populate_routing_tables"):
            with caplog.at_level(logging.WARNING):
                daemon.update(new_links=[])

        assert "not yet active" in caplog.text

    def test_new_links_argument_is_ignored(self, daemon):
        sentinel = object()

        with patch.object(daemon, "_populate_routing_tables") as mock_pop:
            daemon.update(new_links=[sentinel])

        mock_pop.assert_called_once()

    def test_passes_max_workers_to_change_methods(self, daemon, topology):
        topology.status = True

        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_ground_station_routes") as mock_gs, \
             patch.object(daemon, "_apply_satellite_routes") as mock_sat:
            daemon.update(new_links=[], max_workers=2)

        mock_gs.assert_called_once_with(max_workers=2)
        mock_sat.assert_called_once_with(max_workers=2)


class TestStaticRoutingDaemonRemoveSingleNode:
    """Tests for StaticRoutingDaemon.remove(node=<node>)."""

    def test_executes_del_command_for_each_route(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry(dst="10.0.0.1", gw="10.1.0.1")]

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon.remove(node=node)

        mock_batch.assert_called_once()
        lines = mock_batch.call_args[0][1]
        assert len(lines) == 1
        assert "route del" in lines[0]
        assert "10.0.0.0/16" in lines[0]
        assert "via 10.1.0.1" in lines[0]
        assert "dev eth1" in lines[0]

    def test_del_command_format(self, daemon):
        iface = Interface(name="Sat0.3")
        entry = RoutingEntry(
            destination="192.168.0.1",
            interface=iface,
            gateway="192.168.1.254",
            prefix=24,
        )
        node = Node("Sat0")
        node.ipv4_routing_table = [entry]

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon.remove(node=node)

        lines = mock_batch.call_args[0][1]
        assert lines[0] == "route del 192.168.0.0/24 via 192.168.1.254 dev eth3"

    def test_clears_current_routing_table(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry()]

        with patch.object(daemon, "_exec_batch"):
            daemon.remove(node=node)

        assert node.ipv4_routing_table == []

    def test_clears_previous_routing_table(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry()]
        node.ipv4_previous_routing_table = [make_entry()]

        with patch.object(daemon, "_exec_batch"):
            daemon.remove(node=node)

        assert node.ipv4_previous_routing_table == []

    def test_skips_exec_batch_when_table_is_empty(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = []

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon.remove(node=node)

        mock_batch.assert_not_called()

    def test_still_clears_tables_when_table_is_empty(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = []
        node.ipv4_previous_routing_table = [make_entry()]

        daemon.remove(node=node)

        assert node.ipv4_previous_routing_table == []

    def test_does_not_touch_other_nodes(self, daemon):
        target = Node("Sat0")
        target.ipv4_routing_table = [make_entry()]
        other = Node("Sat1")
        other.ipv4_routing_table = [make_entry()]

        with patch.object(daemon, "_exec_batch"):
            daemon.remove(node=target)

        assert other.ipv4_routing_table != []

    def test_multi_entry_table_emits_all_del_commands(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [
            make_entry(dst="10.0.0.1", gw="10.1.0.1"),
            make_entry(dst="10.2.0.1", gw="10.3.0.1", iface_name="Sat0.2"),
        ]

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon.remove(node=node)

        lines = mock_batch.call_args[0][1]
        assert len(lines) == 2


class TestStaticRoutingDaemonRemoveAllNodes:
    """Tests for StaticRoutingDaemon.remove() with no node argument."""

    def test_clears_all_satellites(self, daemon, topology):
        sat0 = Node("Sat0")
        sat0.ipv4_routing_table = [make_entry()]
        sat1 = Node("Sat1")
        sat1.ipv4_routing_table = [make_entry()]
        topology.get_satellites.return_value = [sat0, sat1]
        topology.get_ground_stations.return_value = []

        with patch.object(daemon, "_exec_batch"):
            daemon.remove()

        assert sat0.ipv4_routing_table == []
        assert sat1.ipv4_routing_table == []

    def test_clears_all_ground_stations(self, daemon, topology):
        gs = Node("Gnd0")
        gs.ipv4_routing_table = [make_entry()]
        topology.get_satellites.return_value = []
        topology.get_ground_stations.return_value = [gs]

        with patch.object(daemon, "_exec_batch"):
            daemon.remove()

        assert gs.ipv4_routing_table == []

    def test_exec_batch_called_once_per_node_with_routes(self, daemon, topology):
        sat = Node("Sat0")
        sat.ipv4_routing_table = [make_entry()]
        gs = Node("Gnd0")
        gs.ipv4_routing_table = [make_entry()]
        topology.get_satellites.return_value = [sat]
        topology.get_ground_stations.return_value = [gs]

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon.remove()

        assert mock_batch.call_count == 2

    def test_empty_topology_does_not_call_exec_batch(self, daemon, topology):
        topology.get_satellites.return_value = []
        topology.get_ground_stations.return_value = []

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon.remove()

        mock_batch.assert_not_called()
