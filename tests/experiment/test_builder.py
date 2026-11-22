"""Tests for building a topology from scenario shells and ground stations."""

from __future__ import annotations

from satgonetem.experiment import GroundStationSpec, Scenario, ShellSpec
from satgonetem.experiment.builder import build_scenario_topology
from satgonetem.services.topology_satcom import NetworkConfig


def test_builds_walker_shell_and_ground_station():
    scenario = Scenario(
        name="tiny",
        topology=None,
        duration_s=60,
        shells=(ShellSpec("LEO", planes=3, satellites_per_plane=3),),
        ground_stations=(GroundStationSpec("Berlin", 52.52, 13.405, 0.034),),
    )

    topology = build_scenario_topology(scenario, NetworkConfig(update_time=1))

    assert len(topology.get_satellites()) == 9
    assert len(topology.get_ground_stations()) == 1
    assert topology.get_ground_stations()[0].name == "Gnd9"
    assert topology.address_plan is not None
    assert topology.get_links()


def test_builder_rejects_file_backed_scenario():
    scenario = Scenario(
        name="file-backed",
        topology="test_topology.json",
        duration_s=60,
    )

    try:
        build_scenario_topology(scenario)
    except ValueError as exc:
        assert "shell-based" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected ValueError")
