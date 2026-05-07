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


# ---------------------------------------------------------------------------
# Addressable-satellite helpers
# ---------------------------------------------------------------------------

def _make_iface(node_name, peer_id, peer_iface):
    """Create an Interface named like 'Gnd0.1' with a bidirectional peer."""
    iface = Interface(name=f"{node_name}.{peer_id}")
    iface.peer = peer_iface
    return iface


def _link_nodes(a, b):
    """Add cross-referenced interfaces between two nodes."""
    iface_a = Interface(name=f"{a.name}.{b.id}")
    iface_b = Interface(name=f"{b.name}.{a.id}")
    iface_a.peer = iface_b
    iface_b.peer = iface_a
    # Give them dummy IPv4s so route installation works
    iface_a.ipv4 = f"10.{a.id}.{b.id}.1"
    iface_b.ipv4 = f"10.{a.id}.{b.id}.2"
    a.interfaces.append(iface_a)
    b.interfaces.append(iface_b)


_GS_ID_COUNTER = 0
_SAT_ID_COUNTER = 100


def _make_gs(name, loopback_ip):
    from satgonetem.models.ground_station import GroundStation
    gs = GroundStation(name)
    gs.loopback.ipv4 = loopback_ip
    # Force a unique ID to avoid collisions with satellites
    global _GS_ID_COUNTER
    gs.id = _GS_ID_COUNTER
    _GS_ID_COUNTER += 1
    return gs


def _make_sat(name, loopback_ip, addressable=False):
    from satgonetem.models.satellite import Satellite
    sat = Satellite(name)
    sat.loopback.ipv4 = loopback_ip
    sat.set_addressable(addressable)
    global _SAT_ID_COUNTER
    sat.id = _SAT_ID_COUNTER
    _SAT_ID_COUNTER += 1
    return sat


def _make_line_topology():
    """Return a topology: Gnd0 -- Sat0 -- Sat1 -- Gnd1.

    Sat1 is addressable; Sat0 is not.
    Loopback IPs are spaced by /15 blocks so summarised destinations stay unique.
    """
    gnd0 = _make_gs("Gnd0", "10.0.0.0")
    gnd1 = _make_gs("Gnd1", "10.2.0.0")
    sat0 = _make_sat("Sat0", "10.4.0.0", addressable=False)
    sat1 = _make_sat("Sat1", "10.6.0.0", addressable=True)

    _link_nodes(gnd0, sat0)
    _link_nodes(sat0, sat1)
    _link_nodes(sat1, gnd1)

    graph = nx.Graph()
    for n in (gnd0, gnd1, sat0, sat1):
        graph.add_node(n.id)
    graph.add_edge(gnd0.id, sat0.id, weight=1)
    graph.add_edge(sat0.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, gnd1.id, weight=1)

    topo = MagicMock()
    topo.status = True
    topo.use_file_routes = False
    topo.project_name = "test"
    topo.current_time_step = 0
    topo.get_satellites.return_value = [sat0, sat1]
    topo.get_ground_stations.return_value = [gnd0, gnd1]
    topo.satellites = {sat0.id: sat0, sat1.id: sat1}
    topo.ground_stations = {gnd0.id: gnd0, gnd1.id: gnd1}
    topo.get_current_graph.return_value = graph
    return topo, gnd0, gnd1, sat0, sat1


class TestAddressableSatellites:
    """Tests that static routing treats addressable satellites like ground stations."""

    def test_ground_station_gets_route_to_addressable_satellite(self):
        topo, gnd0, _gnd1, _sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)

        with patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # Gnd0 should have a route to Sat1 (via Sat0)
        routes_to_sat1 = [
            r for r in gnd0.ipv4_routing_table if r.destination == sat1.loopback.ipv4
        ]
        assert len(routes_to_sat1) == 1
        assert routes_to_sat1[0].target_node == sat1.name

    def test_non_addressable_satellite_gets_route_to_addressable_satellite(self):
        topo, _gnd0, _gnd1, sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)

        with patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # Sat0 should have a route to Sat1 (direct)
        routes_to_sat1 = [
            r for r in sat0.ipv4_routing_table if r.destination == sat1.loopback.ipv4
        ]
        assert len(routes_to_sat1) == 1
        assert routes_to_sat1[0].target_node == sat1.name

    def test_addressable_satellite_gets_route_to_other_addressable_satellite(self):
        topo, _gnd0, _gnd1, sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)

        with patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # Nothing to test here because we only have one addressable satellite in
        # the line topology.  We rely on test_addressable_satellite_gets_route_to_ground_station
        # to prove the addressable satellite participates as a source.
        pass

    def test_addressable_satellite_gets_route_to_ground_station(self):
        topo, gnd0, _gnd1, _sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)

        with patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # Sat1 should have a route to Gnd0 (via Sat0)
        routes_to_gnd0 = [
            r for r in sat1.ipv4_routing_table if r.destination == gnd0.loopback.ipv4
        ]
        assert len(routes_to_gnd0) == 1
        assert routes_to_gnd0[0].target_node == gnd0.name

    def test_no_routes_to_non_addressable_satellites(self):
        topo, _gnd0, _gnd1, sat0, _sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)

        with patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # No node should have a route to Sat0's loopback
        all_nodes = list(topo.get_satellites()) + list(topo.get_ground_stations())
        for node in all_nodes:
            routes_to_sat0 = [
                r for r in node.ipv4_routing_table if r.destination == sat0.loopback.ipv4
            ]
            assert len(routes_to_sat0) == 0, (
                f"{node.name} unexpectedly has a route to non-addressable {sat0.name}"
            )

    def test_addressable_satellite_pair_routing(self):
        """Two addressable satellites should have routes to each other."""
        gnd0 = _make_gs("Gnd0", "10.0.0.0")
        sat0 = _make_sat("Sat0", "10.2.0.0", addressable=True)
        sat1 = _make_sat("Sat1", "10.4.0.0", addressable=True)

        _link_nodes(gnd0, sat0)
        _link_nodes(sat0, sat1)

        graph = nx.Graph()
        for n in (gnd0, sat0, sat1):
            graph.add_node(n.id)
        graph.add_edge(gnd0.id, sat0.id, weight=1)
        graph.add_edge(sat0.id, sat1.id, weight=1)

        topo = MagicMock()
        topo.status = True
        topo.use_file_routes = False
        topo.project_name = "test"
        topo.current_time_step = 0
        topo.get_satellites.return_value = [sat0, sat1]
        topo.get_ground_stations.return_value = [gnd0]
        topo.satellites = {sat0.id: sat0, sat1.id: sat1}
        topo.ground_stations = {gnd0.id: gnd0}
        topo.get_current_graph.return_value = graph

        daemon = StaticRoutingDaemon(topo)
        with patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # sat0 should have route to sat1
        routes_s0_to_s1 = [
            r for r in sat0.ipv4_routing_table if r.destination == sat1.loopback.ipv4
        ]
        assert len(routes_s0_to_s1) == 1
        assert routes_s0_to_s1[0].target_node == sat1.name

        # sat1 should have route to sat0
        routes_s1_to_s0 = [
            r for r in sat1.ipv4_routing_table if r.destination == sat0.loopback.ipv4
        ]
        assert len(routes_s1_to_s0) == 1
        assert routes_s1_to_s0[0].target_node == sat0.name

    def test_populate_from_file_with_addressable_satellites(self):
        topo, gnd0, gnd1, sat0, sat1 = _make_line_topology()
        topo.use_file_routes = True

        # Pre-computed routes in the same format _load_routes_from_file expects:
        # {src_id: (distance_dict, path_dict)}
        file_routes = {
            gnd0.id: ({}, {
                gnd0.id: [gnd0.id],
                sat1.id: [gnd0.id, sat0.id, sat1.id],
            }),
            sat0.id: ({}, {
                gnd0.id: [sat0.id, gnd0.id],
                sat1.id: [sat0.id, sat1.id],
            }),
            sat1.id: ({}, {
                gnd0.id: [sat1.id, sat0.id, gnd0.id],
                gnd1.id: [sat1.id, gnd1.id],
            }),
            gnd1.id: ({}, {
                gnd0.id: [gnd1.id, sat1.id, sat0.id, gnd0.id],
                sat1.id: [gnd1.id, sat1.id],
            }),
        }

        daemon = StaticRoutingDaemon(topo)
        with patch.object(daemon, "_load_routes_from_file", return_value=file_routes), \
             patch.object(daemon, "_apply_ground_station_routes"), \
             patch.object(daemon, "_apply_satellite_routes"):
            daemon.init()

        # gnd0 should have route to sat1 from file
        routes_to_sat1 = [
            r for r in gnd0.ipv4_routing_table if r.destination == sat1.loopback.ipv4
        ]
        assert len(routes_to_sat1) == 1

        # sat0 should have route to sat1 from file
        routes_to_sat1_s0 = [
            r for r in sat0.ipv4_routing_table if r.destination == sat1.loopback.ipv4
        ]
        assert len(routes_to_sat1_s0) == 1

        # No routes to non-addressable sat0
        for node in (gnd0, sat0, sat1):
            routes_to_sat0 = [
                r for r in node.ipv4_routing_table if r.destination == sat0.loopback.ipv4
            ]
            assert len(routes_to_sat0) == 0
