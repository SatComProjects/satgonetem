"""Direct contract tests for the extracted topology graph helpers."""

from pathlib import Path
from types import SimpleNamespace

import networkx as nx
import pytest

from satgonetem.services.topology_graph import (
    build_current_graph,
    check_for_updates,
    compute_ground_stations_hash,
    compute_links_hash,
    compute_satellites_hash,
)
from satgonetem.services.topology_satcom import TopologyManager

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)


def _node(node_id):
    return SimpleNamespace(id=node_id, hash_node=lambda: f"node-{node_id}")


def _link(source, target, *, active=True, distance=1.0, delay=1):
    return SimpleNamespace(
        source=source,
        target=target,
        is_active=active,
        distance=distance,
        delay=delay,
    )


def _hash_manager():
    return SimpleNamespace(
        satellites={1: _node(1)},
        ground_stations={2: _node(2)},
        links={frozenset(("a", "b")): _link(_node(3), _node(4))},
        satellites_hash=None,
        ground_stations_hash=None,
        links_hash=None,
    )


class TestHashes:
    def test_hashes_are_stable_for_unchanged_state(self):
        manager = _hash_manager()

        assert compute_satellites_hash(manager) == compute_satellites_hash(manager)
        assert compute_ground_stations_hash(manager) == compute_ground_stations_hash(
            manager
        )
        assert compute_links_hash(manager) == compute_links_hash(manager)

    def test_link_hash_changes_with_distance(self):
        manager = _hash_manager()
        before = compute_links_hash(manager)

        manager.links[frozenset(("a", "b"))].distance = 2.0

        assert compute_links_hash(manager) != before


class TestCheckForUpdates:
    def test_seeds_then_reports_no_change(self):
        manager = _hash_manager()

        assert check_for_updates(manager) == {
            "satellites": True,
            "ground_stations": True,
            "links": True,
        }
        assert check_for_updates(manager) == {
            "satellites": False,
            "ground_stations": False,
            "links": False,
        }

    def test_reports_only_the_changed_category(self):
        manager = _hash_manager()
        check_for_updates(manager)

        manager.satellites[1] = SimpleNamespace(id=1, hash_node=lambda: "changed")

        assert check_for_updates(manager) == {
            "satellites": True,
            "ground_stations": False,
            "links": False,
        }


class TestBuildCurrentGraph:
    @pytest.fixture(scope="class")
    def topology(self):
        return TopologyManager.from_file(TEST_TOPOLOGY)

    def test_requires_a_simulation_manager(self):
        manager = SimpleNamespace(
            simulation_manager=None, nx_adapter=object(), preference=""
        )

        with pytest.raises(ValueError, match="Simulation manager is not set"):
            build_current_graph(manager)

    def test_requires_an_adapter(self):
        manager = SimpleNamespace(
            simulation_manager=object(), nx_adapter=None, preference=""
        )

        with pytest.raises(ValueError, match="nx_adapter is not set"):
            build_current_graph(manager)

    def test_hop_preference_weights_every_edge_one(self, topology):
        topology.preference = "hops_no_preference"

        graph = build_current_graph(topology)

        assert isinstance(graph, nx.Graph)
        assert graph.number_of_edges() > 0
        assert all(data["weight"] == 1 for _, _, data in graph.edges(data=True))

    def test_unknown_preference_falls_back_to_latency(self, topology):
        topology.preference = "not-a-preference"

        graph = build_current_graph(topology)

        assert topology.preference == "latency_no_preference"
        assert all(
            data["weight"] == data.get("distance_km", 1)
            for _, _, data in graph.edges(data=True)
        )
        topology.preference = "latency_prefer_ISLs"

    def test_removes_admin_down_edge(self, topology):
        """H-14: Links with admin_up=False must be excluded from the routable graph."""
        active_link = next(link for link in topology.links.values() if link.is_active)
        u = active_link.source.satcom_object.topology_uniq_id
        v = active_link.target.satcom_object.topology_uniq_id

        assert build_current_graph(topology).has_edge(u, v)

        active_link.admin_up = False
        try:
            assert not build_current_graph(topology).has_edge(u, v)
        finally:
            active_link.admin_up = True
