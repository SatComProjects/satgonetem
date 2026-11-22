"""Tests for shell/ground-station scenarios and generated topology loading."""

from __future__ import annotations

import pytest
import yaml

from satgonetem.experiment import (
    ExperimentRunner,
    GroundStationSpec,
    Scenario,
    ScenarioError,
    ShellSpec,
)


def _write(tmp_path, data):
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_parses_shell_and_ground_station_scenario(tmp_path):
    path = _write(
        tmp_path,
        {
            "name": "generated",
            "duration_s": 60,
            "shells": [
                {
                    "identifier": "LEO",
                    "planes": 3,
                    "satellites_per_plane": 4,
                    "inclination_deg": 53.0,
                    "revs_per_day": 15.0,
                }
            ],
            "ground_stations": [
                {"name": "Berlin", "latitude": 52.52, "longitude": 13.405}
            ],
        },
    )

    scenario = Scenario.from_file(path)

    assert scenario.topology is None
    assert scenario.shells[0].identifier == "LEO"
    assert scenario.ground_stations[0].name == "Berlin"
    assert scenario.to_dict()["shells"][0]["planes"] == 3


def test_topology_and_shells_are_mutually_exclusive(tmp_path):
    path = _write(
        tmp_path,
        {
            "name": "bad",
            "topology": "test_topology.json",
            "duration_s": 60,
            "shells": [{"identifier": "LEO"}],
        },
    )
    with pytest.raises(ScenarioError, match="either topology or shells"):
        Scenario.from_file(path)


def test_ground_stations_require_shells(tmp_path):
    path = _write(
        tmp_path,
        {
            "name": "bad",
            "duration_s": 60,
            "ground_stations": [{"name": "Berlin", "latitude": 0, "longitude": 0}],
        },
    )
    with pytest.raises(ScenarioError, match="requires topology or at least one shell"):
        Scenario.from_file(path)


def test_empty_scenario_is_rejected(tmp_path):
    path = _write(tmp_path, {"name": "empty", "duration_s": 60})
    with pytest.raises(ScenarioError, match="requires topology or at least one shell"):
        Scenario.from_file(path)


def test_duplicate_shell_and_station_names_are_rejected():
    with pytest.raises(ScenarioError, match="unique"):
        Scenario(
            name="dup",
            topology=None,
            duration_s=60,
            shells=(ShellSpec("LEO"), ShellSpec("LEO")),
        )
    with pytest.raises(ScenarioError, match="unique"):
        Scenario(
            name="dup",
            topology=None,
            duration_s=60,
            shells=(ShellSpec("LEO"),),
            ground_stations=(
                GroundStationSpec("Berlin", 0, 0),
                GroundStationSpec("Berlin", 1, 1),
            ),
        )


@pytest.mark.parametrize(
    "section",
    ["traffic", "events", "shells", "ground_stations"],
)
def test_non_mapping_list_items_raise_field_named_errors(tmp_path, section):
    path = _write(
        tmp_path,
        {
            "name": "bad-list-item",
            "duration_s": 60,
            section: [None],
        },
    )

    with pytest.raises(ScenarioError) as excinfo:
        Scenario.from_file(path)

    message = str(excinfo.value)
    assert f"scenario.{section}[0]" in message
    assert "must be a mapping" in message


def test_runner_passes_none_path_for_generated_scenario(tmp_path):
    scenario = Scenario(
        name="generated",
        topology=None,
        duration_s=1,
        shells=(ShellSpec("LEO", planes=3, satellites_per_plane=3),),
    )
    calls = []

    class FakeTopology:
        current_time_step = 0
        update_time = 1
        satellites = {}
        ground_stations = {}
        links = {}

        def next_step(self):
            self.current_time_step += 1

    runner = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: (
            calls.append((path, config)) or FakeTopology()
        ),
        collector_factory=lambda topo: type(
            "C", (), {"sample": lambda self: {"step": 0, "links": []}}
        )(),
    )
    runner.run(output_dir=tmp_path / "run", live=False, realtime=False)

    assert calls[0][0] is None
