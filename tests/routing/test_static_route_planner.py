"""Tests for the static route table planner."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.routing.peer_interface_index import PeerInterfaceIndex
from satgonetem.routing.static_route_planner import StaticRoutePlanner


def _make_node(name, node_id, loopback_ip):
    node = Node(name)
    node.id = node_id
    node.loopback.ipv4 = loopback_ip
    return node


def _make_addressable_sat(name, node_id, loopback_ip):
    return SimpleNamespace(
        id=node_id,
        name=name,
        loopback=SimpleNamespace(ipv4=loopback_ip),
        is_addressable=lambda: True,
        interfaces=[],
    )


def _make_interface(src_name, peer_id, peer_ip):
    iface = Interface(name=f"{src_name}.{peer_id}")
    peer = Interface(name=f"peer.{peer_id}")
    peer.ipv4 = peer_ip
    iface.peer = peer
    return iface


def _make_planner(topology=None):
    topology = topology or MagicMock()
    return StaticRoutePlanner(topology, PeerInterfaceIndex(topology))


class TestPopulateFromFile:
    def test_clears_reuse_cache_before_installing_file_routes(self):
        src = _make_node("Gnd0", 1, "10.0.0.1")
        dst = _make_addressable_sat("Sat0", 2, "10.0.0.2")
        iface = _make_interface("Gnd0", 2, "10.9.0.2")
        planner = _make_planner()
        planner._source_signatures[9] = ["stale"]
        planner._entry_cache[9] = {"stale": object()}
        load_routes = MagicMock(return_value={1: ({}, {2: [1, 2]})})

        planner.populate_from_file([src], [dst], {1: {2: iface}}, {}, load_routes)

        assert planner.source_signatures == {}
        assert planner.entry_cache == {}
        load_routes.assert_called_once_with()
        assert len(src.ipv4_routing_table) == 1
        entry = src.ipv4_routing_table[0]
        assert entry.destination == "10.0.0.2"
        assert entry.gateway == "10.9.0.2"
        assert entry.interface is iface
        assert entry.prefix == 32

    def test_skips_destinations_without_a_path_and_self(self):
        src = _make_node("Gnd0", 1, "10.0.0.1")
        dst = _make_addressable_sat("Sat0", 2, "10.0.0.2")
        planner = _make_planner()

        planner.populate_from_file(
            [src],
            [dst],
            {1: {}},
            {},
            MagicMock(return_value={1: ({}, {2: None})}),
        )

        assert src.ipv4_routing_table == []


class TestInstallSourceRoutes:
    def test_cache_hit_aliases_previous_and_current_tables(self):
        src = _make_node("Sat0", 1, "10.0.0.1")
        dst = SimpleNamespace(id=2, name="Gnd0")
        iface = _make_interface("Sat0", 2, "10.9.0.2")
        planner = _make_planner()
        signature = [(2, "10.0.0.2", "10.9.0.2", 2)]
        kwargs = dict(
            node=src,
            src_id=1,
            src_ifaces={2: iface},
            src_loopback="10.0.0.1",
            src_name="Sat0",
            dst_array=[dst],
            dst_ids=[2],
            signature=signature,
            loopback_ips={2: "10.0.0.2"},
            prefix=32,
        )

        planner.install_source_routes(**kwargs)
        first_table = src.ipv4_routing_table
        first_entry = first_table[0]

        planner.install_source_routes(**kwargs)

        assert src.ipv4_routing_table is not first_table
        assert src.ipv4_previous_routing_table is src.ipv4_routing_table
        assert src.ipv4_routing_table[0] is first_entry

    def test_changed_signature_reuses_only_unaffected_entries(self):
        src = _make_node("Sat0", 1, "10.0.0.1")
        dst_a = SimpleNamespace(id=2, name="Gnd0")
        dst_b = SimpleNamespace(id=3, name="Gnd1")
        iface_a = _make_interface("Sat0", 2, "10.9.0.2")
        iface_b = _make_interface("Sat0", 3, "10.9.0.3")
        planner = _make_planner()
        base_kwargs = dict(
            node=src,
            src_id=1,
            src_ifaces={2: iface_a, 3: iface_b},
            src_loopback="10.0.0.1",
            src_name="Sat0",
            dst_array=[dst_a, dst_b],
            dst_ids=[2, 3],
            loopback_ips={2: "10.0.0.2", 3: "10.0.0.3"},
            prefix=32,
        )
        planner.install_source_routes(
            signature=[
                (2, "10.0.0.2", "10.9.0.2", 2),
                (3, "10.0.0.3", "10.9.0.3", 3),
            ],
            **base_kwargs,
        )
        old_a, old_b = src.ipv4_routing_table

        planner.install_source_routes(
            signature=[
                (2, "10.0.0.2", "10.9.0.2", 2),
                (3, "10.0.0.3", "10.9.0.99", 3),
            ],
            **base_kwargs,
        )

        new_a, new_b = src.ipv4_routing_table
        assert new_a is old_a
        assert new_b is not old_b
        assert new_b.gateway == "10.9.0.99"


class TestPopulateTables:
    def test_file_mode_delegates_to_file_population(self):
        topology = MagicMock()
        topology.use_file_routes = True
        topology.get_ground_stations.return_value = ["gs"]
        topology.get_satellites.return_value = ["sat"]
        topology.interface_version = MagicMock()
        planner = _make_planner(topology)
        load_routes = MagicMock()

        with patch.object(planner, "populate_from_file") as pop_file:
            planner.populate_tables(MagicMock(), load_routes)

        pop_file.assert_called_once_with(["gs"], ["sat"], {}, {}, load_routes)

    def test_graph_mode_runs_inside_gc_disabled_context(self):
        topology = MagicMock()
        topology.use_file_routes = False
        topology.get_ground_stations.return_value = ["gs"]
        topology.get_satellites.return_value = ["sat"]
        topology.get_current_graph.return_value = "graph"
        planner = _make_planner(topology)
        events = []

        @contextmanager
        def gc_disabled():
            events.append("enter")
            yield
            events.append("exit")

        with patch.object(planner, "populate_dijkstra_igraph") as pop_ig:
            planner.populate_tables(gc_disabled, MagicMock())

        assert events == ["enter", "exit"]
        pop_ig.assert_called_once_with("graph", ["gs"], ["sat"], {}, {})


class TestLoadRoutesFromFile:
    def test_reads_and_coerces_keys_and_values(self, tmp_path, monkeypatch):
        # The file format is {src: ({dst: distance}, {dst: [path]})}, so build
        # the nested pair explicitly.
        payload = {"1": ({"2": 4}, {"3": [1, 2, 3]})}
        project_dir = tmp_path / "projects" / "demo" / "ts5"
        project_dir.mkdir(parents=True)
        (project_dir / "routes_emulator.txt").write_text(json.dumps(payload))
        monkeypatch.chdir(tmp_path)

        topology = MagicMock()
        topology.project_name = "demo"
        topology.current_time_step = 5
        planner = _make_planner(topology)

        routes = planner.load_routes_from_file()

        assert routes == {1: ({2: 4.0}, {3: [1, 2, 3]})}
        assert isinstance(next(iter(routes[1][1])), int)
        assert isinstance(routes[1][0][2], float)
