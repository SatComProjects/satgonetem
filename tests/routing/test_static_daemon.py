"""Tests for StaticRoutingDaemon."""

import logging

import grpc
import pytest
import networkx as nx
from unittest.mock import MagicMock, patch

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.models.routing_entry import RoutingEntry
from satgonetem.proto import netem_pb2
from satgonetem.routing import static_daemon
from satgonetem.routing.static_daemon import RouteOperation, StaticRoutingDaemon


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

    def test_applies_all_routes_once(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(
                 daemon, "_apply_all_routes",
                 return_value=static_daemon._BatchOutcome(),
             ) as mock_apply:
            daemon.init()

        mock_apply.assert_called_once_with(max_workers=128)

    def test_returns_false_on_exception(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(
                 daemon, "_apply_all_routes", side_effect=RuntimeError("boom")
             ):
            result = daemon.init()

        assert result is False

    def test_passes_max_workers_to_change_method(self, daemon):
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(
                 daemon, "_apply_all_routes",
                 return_value=static_daemon._BatchOutcome(),
             ) as mock_apply:
            daemon.init(max_workers=8)

        mock_apply.assert_called_once_with(max_workers=8)

    def test_returns_false_when_any_node_fails(self, daemon):
        outcome = static_daemon._BatchOutcome(error_nodes={"Sat0"})
        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_all_routes", return_value=outcome):
            assert daemon.init() is False


class TestStaticRoutingDaemonUpdate:
    """Tests for StaticRoutingDaemon.update()."""

    def test_populates_routing_tables(self, daemon):
        with patch.object(daemon, "_populate_routing_tables") as mock_pop:
            daemon.update(new_links=[])

        mock_pop.assert_called_once()

    def test_applies_routes_when_topology_active(self, daemon, topology):
        topology.status = True

        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_all_routes") as mock_apply:
            daemon.update(new_links=[])

        mock_apply.assert_called_once()

    def test_skips_route_application_when_topology_inactive(self, daemon, topology):
        topology.status = False

        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_all_routes") as mock_apply:
            daemon.update(new_links=[])

        mock_apply.assert_not_called()

    def test_logs_warning_when_topology_inactive(self, daemon, topology, caplog):
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

    def test_passes_max_workers_to_change_method(self, daemon, topology):
        topology.status = True

        with patch.object(daemon, "_populate_routing_tables"), \
             patch.object(daemon, "_apply_all_routes") as mock_apply:
            daemon.update(new_links=[], max_workers=2)

        mock_apply.assert_called_once_with(max_workers=2)


class TestStaticRoutingDaemonRemoveSingleNode:
    """Tests for StaticRoutingDaemon.remove(node=<node>)."""

    def test_executes_del_command_for_each_route(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry(dst="10.0.0.1", gw="10.1.0.1")]
        node.interfaces.append(node.ipv4_routing_table[0].interface)

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
        node.interfaces.append(entry.interface)

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
        node.interfaces.extend(entry.interface for entry in node.ipv4_routing_table)

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
        sat.interfaces.append(sat.ipv4_routing_table[0].interface)
        gs = Node("Gnd0")
        gs.ipv4_routing_table = [make_entry()]
        gs.interfaces.append(gs.ipv4_routing_table[0].interface)
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

        with patch.object(daemon, "_apply_all_routes"):
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

        with patch.object(daemon, "_apply_all_routes"):
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

        with patch.object(daemon, "_apply_all_routes"):
            daemon.init()

        # Nothing to test here because we only have one addressable satellite in
        # the line topology.  We rely on test_addressable_satellite_gets_route_to_ground_station
        # to prove the addressable satellite participates as a source.
        pass

    def test_addressable_satellite_gets_route_to_ground_station(self):
        topo, gnd0, _gnd1, _sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)

        with patch.object(daemon, "_apply_all_routes"):
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

        with patch.object(daemon, "_apply_all_routes"):
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
        with patch.object(daemon, "_apply_all_routes"):
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
             patch.object(daemon, "_apply_all_routes"):
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


class TestPopulateDijkstraIgraph:
    """Correctness tests for the igraph-backed Dijkstra implementation."""

    def test_igraph_version_matches_networkx_on_line_topology(self):
        """Both backends must install the same routes on the line topology."""
        topo, gnd0, gnd1, sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)
        gs_ifaces, sat_ifaces = daemon._prebuild_iface_maps()
        graph = topo.get_current_graph()

        daemon._populate_dijkstra(
            graph,
            [gnd0, gnd1],
            [sat0, sat1],
            gs_ifaces,
            sat_ifaces,
        )
        nx_routes = {
            node.id: sorted(
                (r.destination, r.target_node, r.gateway, r.interface.name)
                for r in node.ipv4_routing_table
            )
            for node in (gnd0, gnd1, sat0, sat1)
        }

        for node in (gnd0, gnd1, sat0, sat1):
            node.ipv4_routing_table.clear()

        daemon._populate_dijkstra_igraph(
            graph,
            [gnd0, gnd1],
            [sat0, sat1],
            gs_ifaces,
            sat_ifaces,
        )
        ig_routes = {
            node.id: sorted(
                (r.destination, r.target_node, r.gateway, r.interface.name)
                for r in node.ipv4_routing_table
            )
            for node in (gnd0, gnd1, sat0, sat1)
        }

        assert nx_routes == ig_routes


class TestRouteDiff:
    """Tests for the incremental route diff emitted by the daemon."""

    def test_removed_destination_emits_delete(self, daemon):
        stale = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )

        lines = daemon._emit_batch_commands([stale], [])

        assert lines == ["route del 10.0.0.0/16 via 10.1.0.1 dev eth1"]

    def test_removed_destination_deletes_when_device_is_present(self, daemon):
        stale = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )

        lines = daemon._emit_batch_commands([stale], [], live_devs={"eth1"})

        assert lines == ["route del 10.0.0.0/16 via 10.1.0.1 dev eth1"]

    def test_removed_destination_skips_delete_when_device_is_gone(self, daemon):
        stale = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )

        lines = daemon._emit_batch_commands([stale], [], live_devs={"eth2"})

        assert lines == []

    def test_empty_live_devs_skips_all_deletions(self, daemon):
        stale = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )

        assert daemon._emit_batch_commands([stale], [], live_devs=set()) == []

    def test_mixed_batch_keeps_live_deletions_and_additions(self, daemon):
        gone = make_entry(
            dst="10.1.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )
        live = make_entry(
            dst="10.2.0.1", gw="10.1.0.2", iface_name="Sat0.2", prefix=16
        )
        new = make_entry(
            dst="10.3.0.1", gw="10.1.0.3", iface_name="Sat0.2", prefix=16
        )

        lines = daemon._emit_batch_commands(
            [gone, live], [new], live_devs={"eth2"}
        )

        assert lines == [
            "route del 10.2.0.0/16 via 10.1.0.2 dev eth2",
            "route replace 10.3.0.0/16 via 10.1.0.3 dev eth2",
        ]

    def test_changed_next_hop_replaces_without_deleting(self, daemon):
        old = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )
        new = make_entry(
            dst="10.0.0.1", gw="10.1.0.2", iface_name="Sat0.2", prefix=16
        )

        lines = daemon._emit_batch_commands([old], [new])

        assert lines == [
            "route replace 10.0.0.0/16 via 10.1.0.2 dev eth2",
        ]

    def test_added_destination_still_emits_replace(self, daemon):
        new = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )

        lines = daemon._emit_batch_commands([], [new])

        assert lines == ["route replace 10.0.0.0/16 via 10.1.0.1 dev eth1"]

    def test_unchanged_table_emits_nothing(self, daemon):
        entry = make_entry(
            dst="10.0.0.1", gw="10.1.0.1", iface_name="Sat0.1", prefix=16
        )

        assert daemon._emit_batch_commands([entry], [entry]) == []

    def test_apply_satellite_routes_executes_stale_route_delete(
        self, daemon, topology
    ):
        node = Node("Sat0")
        entry = make_entry()
        node.interfaces.append(entry.interface)
        node.ipv4_previous_routing_table = [entry]
        node.ipv4_routing_table = []
        topology.satellites = {node.id: node}

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon._apply_satellite_routes(max_workers=1)

        mock_batch.assert_called_once()
        lines = mock_batch.call_args[0][1]
        assert lines == ["route del 10.0.0.0/16 via 10.1.0.1 dev eth1"]

    def test_apply_satellite_routes_skips_deletes_for_removed_interfaces(
        self, daemon, topology
    ):
        """Routes on a torn-down link are already purged by the kernel."""
        node = Node("Sat0")
        node.ipv4_previous_routing_table = [make_entry()]
        node.ipv4_routing_table = []
        topology.satellites = {node.id: node}

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon._apply_satellite_routes(max_workers=1)

        mock_batch.assert_not_called()

    def test_apply_ground_station_routes_skips_deletes_for_removed_interfaces(
        self, daemon, topology
    ):
        node = Node("Gnd0")
        node.ipv4_previous_routing_table = [make_entry()]
        node.ipv4_routing_table = []
        topology.ground_stations = {node.id: node}

        with patch.object(daemon, "_exec_batch") as mock_batch:
            daemon._apply_ground_station_routes(max_workers=1)

        mock_batch.assert_not_called()


class TestRouteReuse:
    """Tests for route-entry reuse across topology updates."""

    def test_equalize_keeps_previous_table_by_reference(self, daemon, topology):
        node = Node("Sat0")
        table = [make_entry()]
        node.ipv4_routing_table = table
        topology.get_satellites.return_value = [node]
        topology.get_ground_stations.return_value = []

        daemon._equalize_routing_tables()

        assert node.ipv4_previous_routing_table is table
        assert node.ipv4_routing_table == []
        assert node.ipv4_routing_table is not table

    def test_identical_tables_skip_the_diff(self, daemon):
        table = [make_entry()]

        assert daemon._emit_batch_commands(table, table) == []

    def test_update_reuses_entries_when_routes_are_unchanged(self):
        topo, gnd0, gnd1, sat0, sat1 = _make_line_topology()
        daemon = StaticRoutingDaemon(topo)
        nodes = [gnd0, gnd1, sat0, sat1]

        with patch.object(daemon, "_apply_all_routes"):
            daemon.init()

        before = {node.id: list(node.ipv4_routing_table) for node in nodes}

        with patch.object(daemon, "_apply_all_routes"):
            daemon.update([])

        for node in nodes:
            assert len(node.ipv4_routing_table) == len(before[node.id])
            for old_entry, new_entry in zip(
                before[node.id], node.ipv4_routing_table
            ):
                assert new_entry is old_entry
            assert node.ipv4_previous_routing_table is node.ipv4_routing_table

    def test_update_reuses_only_unaffected_entries(self):
        topo, gnd0, gnd1, sat0, _sat1 = _make_line_topology()
        _link_nodes(gnd0, gnd1)
        graph = topo.get_current_graph.return_value
        graph.add_edge(gnd0.id, gnd1.id, weight=100)

        daemon = StaticRoutingDaemon(topo)
        with patch.object(daemon, "_apply_all_routes"):
            daemon.init()

        old_sat0_to_gnd0 = next(
            entry
            for entry in sat0.ipv4_routing_table
            if entry.target_node == "Gnd0"
        )
        old_gnd0_to_gnd1 = next(
            entry
            for entry in gnd0.ipv4_routing_table
            if entry.target_node == "Gnd1"
        )

        graph[gnd0.id][gnd1.id]["weight"] = 0.1

        with patch.object(daemon, "_apply_all_routes"):
            daemon.update([])

        new_sat0_to_gnd0 = next(
            entry
            for entry in sat0.ipv4_routing_table
            if entry.target_node == "Gnd0"
        )
        new_gnd0_to_gnd1 = next(
            entry
            for entry in gnd0.ipv4_routing_table
            if entry.target_node == "Gnd1"
        )

        assert new_sat0_to_gnd0 is old_sat0_to_gnd0
        assert new_gnd0_to_gnd1 is not old_gnd0_to_gnd1


class TestRouteBuildGc:
    """Tests for the route-build GC suppression helper."""

    def test_gc_disabled_restores_previous_enabled_state(self):
        with patch.object(static_daemon.gc, "isenabled", return_value=True), \
             patch.object(static_daemon.gc, "disable") as disable, \
             patch.object(static_daemon.gc, "enable") as enable:
            with static_daemon._gc_disabled():
                pass

        disable.assert_called_once()
        enable.assert_called_once()

    def test_gc_disabled_preserves_already_disabled_state(self):
        with patch.object(static_daemon.gc, "isenabled", return_value=False), \
             patch.object(static_daemon.gc, "disable") as disable, \
             patch.object(static_daemon.gc, "enable") as enable:
            with static_daemon._gc_disabled():
                pass

        disable.assert_not_called()
        enable.assert_not_called()


class TestInterfaceMapCache:
    """Tests for the interface-map cache used by routing updates."""

    def test_iface_maps_are_reused_until_interface_version_changes(self):
        gs = Node("Gnd0")
        gs.interfaces = [Interface(name="Gnd0.1")]
        sat = Node("Sat1")
        sat.interfaces = [Interface(name="Sat1.0")]

        topology = MagicMock()
        topology.interface_version = 1
        topology.ground_stations = {gs.id: gs}
        topology.satellites = {sat.id: sat}
        daemon = StaticRoutingDaemon(topology)

        first = daemon._prebuild_iface_maps()
        second = daemon._prebuild_iface_maps()

        assert first is second

        topology.interface_version = 2
        third = daemon._prebuild_iface_maps()

        assert third is not first
        assert third[0][gs.id] is not first[0][gs.id]


# ---------------------------------------------------------------------------
# RouteInstall batch tests
# ---------------------------------------------------------------------------


class _UnimplementedRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNIMPLEMENTED


class _InternalRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.INTERNAL


def _route_response(request, errors=None, durations=None, missing=()):
    """Build a successful RouteInstallResponse with optional node failures."""
    response = netem_pb2.RouteInstallResponse()
    response.status.code = netem_pb2.StatusCode.OK
    for name in request.nodes:
        if name in missing:
            continue
        result = response.results[name]
        if errors and name in errors:
            result.status.code = netem_pb2.StatusCode.ERROR
            result.status.error = errors[name]
            continue
        result.status.code = netem_pb2.StatusCode.OK
        result.applied = len(request.nodes[name].routes)
        result.durationMicros = (durations or {}).get(name, 1000)
    return response


class FakeRouteInstallClient:
    """Minimal stand-in for the generated NetemStub RouteInstall method."""

    def __init__(self, error=None, response_factory=None):
        self.requests = []
        self.error = error
        self.response_factory = response_factory or _route_response

    def RouteInstall(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response_factory(request)


def _make_batch_node(name, curr, prev=None, client=None, project_id="prj"):
    """Build a node with live interfaces for its previous and current tables."""
    node = Node(name)
    node.grpc_client = client
    node.project_id = project_id
    node.ipv4_previous_routing_table = prev if prev is not None else []
    node.ipv4_routing_table = list(curr)
    live = {}
    for entry in list(node.ipv4_previous_routing_table) + list(curr):
        live[entry.interface.get_iname()] = entry.interface
    node.interfaces = list(live.values())
    return node


class TestRouteInstallBatch:
    def test_request_has_structured_operations_and_skips_unchanged_nodes(
        self, topology
    ):
        client = FakeRouteInstallClient()
        old = make_entry(dst="10.0.0.1", gw="192.0.2.1", iface_name="Sat0.1")
        new = make_entry(dst="10.1.0.1", gw="192.0.2.2", iface_name="Sat0.3")
        sat = _make_batch_node("Sat0", [new], prev=[old], client=client)
        unchanged = make_entry(dst="10.2.0.1", iface_name="Sat1.1")
        sat1 = _make_batch_node("Sat1", [unchanged], prev=[unchanged], client=client)
        topology.satellites = {sat.id: sat, sat1.id: sat1}
        topology.ground_stations = {}
        daemon = StaticRoutingDaemon(topology)

        outcome = daemon._apply_all_routes(max_workers=1)

        assert len(client.requests) == 1
        request = client.requests[0]
        assert request.prjId == "prj"
        assert request.maxConcurrency == 0
        assert set(request.nodes.keys()) == {"Sat0"}
        specs = list(request.nodes["Sat0"].routes)
        assert [
            (spec.action, spec.destination, spec.gateway, spec.device)
            for spec in specs
        ] == [
            (netem_pb2.ROUTE_DELETE, "10.0.0.0/16", "192.0.2.1", "eth1"),
            (netem_pb2.ROUTE_REPLACE, "10.1.0.0/16", "192.0.2.2", "eth3"),
        ]
        assert outcome.ok_nodes == {"Sat0"}

    def test_install_route_batches_accepts_node_names(self, topology):
        client = FakeRouteInstallClient()
        node = _make_batch_node("Sat0", [make_entry()], client=client)
        topology.satellites = {node.id: node}
        daemon = StaticRoutingDaemon(topology)
        ops = daemon._emit_route_operations([], node.ipv4_routing_table)

        outcome = daemon._install_route_batches({"Sat0": ops}, max_workers=1)

        assert len(client.requests) == 1
        assert "Sat0" in client.requests[0].nodes
        assert outcome.ok_nodes == {"Sat0"}

    def test_ground_station_and_satellite_share_one_request(self, topology):
        client = FakeRouteInstallClient()
        gs = _make_batch_node(
            "Gnd0", [make_entry(iface_name="Gnd0.1")], client=client
        )
        sat = _make_batch_node(
            "Sat0", [make_entry(iface_name="Sat0.1")], client=client
        )
        topology.ground_stations = {gs.id: gs}
        topology.satellites = {sat.id: sat}
        daemon = StaticRoutingDaemon(topology)

        daemon._apply_all_routes(max_workers=1)

        assert len(client.requests) == 1
        assert set(client.requests[0].nodes.keys()) == {"Gnd0", "Sat0"}

    def test_emit_route_operations_allows_on_link_gateway(self, daemon):
        entry = make_entry(gw="", iface_name="Sat0.2")

        ops = daemon._emit_route_operations([], [entry])

        assert ops == [RouteOperation("replace", "10.0.0.0/16", "", "eth2")]
        assert (
            daemon._format_route_operation(ops[0])
            == "route replace 10.0.0.0/16 dev eth2"
        )

    def test_unimplemented_falls_back_and_does_not_retry(
        self, topology, caplog
    ):
        client = FakeRouteInstallClient(error=_UnimplementedRpcError())
        node = _make_batch_node("Sat0", [make_entry()], client=client)
        topology.satellites = {node.id: node}
        daemon = StaticRoutingDaemon(topology)

        with patch.object(daemon, "_exec_batch") as mock_exec:
            with caplog.at_level(logging.WARNING):
                first = daemon._apply_all_routes(max_workers=2)
                second = daemon._apply_all_routes(max_workers=2)

        assert len(client.requests) == 1
        assert mock_exec.call_count == 2
        assert first.applied == 1
        assert second.applied == 1
        assert daemon._route_install_supported is False
        assert "legacy per-node NodeRun route path" in caplog.text

    def test_other_rpc_errors_propagate(self, topology):
        client = FakeRouteInstallClient(error=_InternalRpcError())
        node = _make_batch_node("Sat0", [make_entry()], client=client)
        topology.satellites = {node.id: node}
        daemon = StaticRoutingDaemon(topology)

        with pytest.raises(_InternalRpcError):
            daemon._apply_all_routes(max_workers=1)

        assert daemon._route_install_supported is True

    def test_per_node_error_does_not_stop_other_nodes(self, topology, caplog):
        client = FakeRouteInstallClient(
            response_factory=lambda req: _route_response(
                req, errors={"Sat1": "boom"}
            )
        )
        sat0 = _make_batch_node("Sat0", [make_entry()], client=client)
        sat1 = _make_batch_node("Sat1", [make_entry()], client=client)
        topology.satellites = {sat0.id: sat0, sat1.id: sat1}
        daemon = StaticRoutingDaemon(topology)

        with caplog.at_level(logging.ERROR):
            outcome = daemon._apply_all_routes(max_workers=1)

        assert outcome.ok_nodes == {"Sat0"}
        assert outcome.error_nodes == {"Sat1"}
        assert outcome.applied == 1
        assert "Sat1" in caplog.text
        assert "boom" in caplog.text

    def test_missing_result_is_a_node_error(self, topology, caplog):
        client = FakeRouteInstallClient(
            response_factory=lambda req: _route_response(req, missing={"Sat0"})
        )
        sat0 = _make_batch_node("Sat0", [make_entry()], client=client)
        topology.satellites = {sat0.id: sat0}
        daemon = StaticRoutingDaemon(topology)

        with caplog.at_level(logging.ERROR):
            outcome = daemon._apply_all_routes(max_workers=1)

        assert outcome.error_nodes == {"Sat0"}
        assert "no result" in caplog.text

    def test_requests_are_chunked_by_node_count(self, topology, monkeypatch):
        monkeypatch.setattr(static_daemon, "MAX_ROUTE_NODES_PER_REQUEST", 2)
        client = FakeRouteInstallClient()
        nodes = [
            _make_batch_node(
                f"Sat{i}",
                [make_entry(dst=f"10.{i}.0.1", iface_name=f"Sat{i}.1")],
                client=client,
            )
            for i in range(5)
        ]
        topology.satellites = {node.id: node for node in nodes}
        daemon = StaticRoutingDaemon(topology)

        outcome = daemon._apply_all_routes(max_workers=1)

        assert len(client.requests) == 3
        sent = []
        for request in client.requests:
            sent.extend(request.nodes.keys())
        assert sorted(sent) == sorted(node.name for node in nodes)
        assert outcome.ok_nodes == {node.name for node in nodes}

    def test_requests_are_chunked_by_serialized_size(self, topology, monkeypatch):
        monkeypatch.setattr(static_daemon, "MAX_ROUTE_REQUEST_BYTES", 100)
        client = FakeRouteInstallClient()
        nodes = []
        for i in range(3):
            entries = [
                make_entry(
                    dst=f"10.{i}.{j}.1",
                    iface_name=f"Sat{i}.1",
                )
                for j in range(5)
            ]
            nodes.append(_make_batch_node(f"Sat{i}", entries, client=client))
        topology.satellites = {node.id: node for node in nodes}
        daemon = StaticRoutingDaemon(topology)

        daemon._apply_all_routes(max_workers=1)

        assert len(client.requests) == 3

    def test_remove_all_sends_one_delete_batch(self, topology):
        client = FakeRouteInstallClient()
        sat = _make_batch_node(
            "Sat0", [make_entry(dst="10.0.0.1", iface_name="Sat0.1")], client=client
        )
        gs = _make_batch_node(
            "Gnd0", [make_entry(dst="10.1.0.1", iface_name="Gnd0.1")], client=client
        )
        topology.get_satellites.return_value = [sat]
        topology.get_ground_stations.return_value = [gs]
        daemon = StaticRoutingDaemon(topology)

        daemon.remove(max_workers=1)

        assert len(client.requests) == 1
        request = client.requests[0]
        assert set(request.nodes.keys()) == {"Sat0", "Gnd0"}
        for name in ("Sat0", "Gnd0"):
            specs = list(request.nodes[name].routes)
            assert specs
            assert all(
                spec.action == netem_pb2.ROUTE_DELETE for spec in specs
            )
        assert sat.ipv4_routing_table == []
        assert gs.ipv4_routing_table == []

    def test_remove_skips_deletes_for_missing_devices(self, topology):
        client = FakeRouteInstallClient()
        node = Node("Sat0")
        node.grpc_client = client
        node.project_id = "prj"
        node.ipv4_routing_table = [make_entry()]
        topology.get_satellites.return_value = [node]
        topology.get_ground_stations.return_value = []
        daemon = StaticRoutingDaemon(topology)

        daemon.remove(max_workers=1)

        assert client.requests == []
        assert node.ipv4_routing_table == []


class TestStaticRoutingDaemonFailureTracking:
    """Failed batches must be retried instead of treated as installed."""

    def test_failed_node_is_tracked(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry()]
        outcome = static_daemon._BatchOutcome(error_nodes={"Sat0"})

        with patch.object(daemon, "_install_route_batches", return_value=outcome):
            daemon._apply_route_changes([node])

        assert "Sat0" in daemon._failed_nodes

    def test_successful_node_clears_failure(self, daemon):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry()]
        daemon._failed_nodes.add("Sat0")
        outcome = static_daemon._BatchOutcome(ok_nodes={"Sat0"})

        with patch.object(daemon, "_install_route_batches", return_value=outcome):
            daemon._apply_route_changes([node])

        assert "Sat0" not in daemon._failed_nodes

    def test_equalize_forces_retry_for_failed_node(self, daemon, topology):
        node = Node("Sat0")
        node.ipv4_routing_table = [make_entry()]
        daemon._failed_nodes.add("Sat0")
        topology.get_satellites.return_value = [node]
        topology.get_ground_stations.return_value = []

        daemon._equalize_routing_tables()

        assert node.ipv4_previous_routing_table == []
        assert node.ipv4_routing_table == []
