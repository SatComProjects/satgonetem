"""Phase 0 tests: stage timings and the topology_events feature flag."""

from __future__ import annotations

from types import SimpleNamespace

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

TEST_TOPOLOGY_PATH = "topology_files/test_topology.json"


def test_network_config_topology_events_defaults_off():
    assert NetworkConfig().topology_events is False


def test_network_config_accepts_topology_events():
    assert NetworkConfig(topology_events=True).topology_events is True


def test_network_config_default_reconcile_interval():
    assert NetworkConfig().event_reconcile_interval == 30


def test_apply_network_config_sets_topology_events():
    manager = TopologyManager.__new__(TopologyManager)
    manager.simulation_manager = SimpleNamespace(project_name="Phase0")

    manager._apply_network_config(NetworkConfig(topology_events=True))

    assert manager.topology_events is True


def test_last_stage_timings_are_populated_after_a_tick():
    manager = TopologyManager.from_file(TEST_TOPOLOGY_PATH)
    assert manager.get_last_stage_timings() == {}

    manager.next_step()

    timings = manager.get_last_stage_timings()
    for stage in (
        "tick",
        "position_sync",
        "link_sync",
        "event_collection",
        "interface_sync",
        "total",
    ):
        assert stage in timings
        assert timings[stage] >= 0.0
    assert timings["total"] >= timings["tick"]


def test_last_stage_timings_returns_a_copy():
    manager = TopologyManager.from_file(TEST_TOPOLOGY_PATH)
    manager.next_step()

    timings = manager.get_last_stage_timings()
    timings["total"] = -1.0

    assert manager.get_last_stage_timings()["total"] >= 0.0


def test_topology_events_round_trips_through_file(tmp_path):
    manager = TopologyManager.from_file(TEST_TOPOLOGY_PATH)
    manager.topology_events = True
    manager.event_reconcile_interval = 7
    path = tmp_path / "phase0_topology.json"

    manager.to_file(str(path))
    restored = TopologyManager.from_file(str(path))

    assert restored.topology_events is True
    assert restored.event_reconcile_interval == 7
