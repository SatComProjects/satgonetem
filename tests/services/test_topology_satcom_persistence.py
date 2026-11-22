"""Persistence tests for TopologyManager runtime-state round-trips.

A topology built from a SimulationManager (rather than a SimulationProperty)
is saved through ``to_file`` by serialising its runtime state.  Reloading it
must restore the simulation window and the programmatically added nodes.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from satgonetem.services.topology_satcom import TopologyManager
from satgonetem.utils.project_builder import (
    add_custom_ground_station,
    create_and_load_simulation,
    create_custom_ground_station,
    create_custom_satellite,
)
from satgonetem.utils.project_fixtures import create_empty_project

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)

START_DATE = datetime(2024, 1, 1, 0, 0, 0)
END_DATE = datetime(2024, 1, 1, 1, 0, 0)

_ORIGINAL_DATA_FILE = "/tmp/ground_stations.txt"
_BENIGN_CSV = "0,OnlyStation,0.0,0.0,0.0\n"
_OTHER_CSV = "0,OtherStation,1.0,1.0,0.0\n"

_CUSTOM_SATELLITE = {
    "name": "AuditSat-001",
    "tle_line1": "1 25544U 98067A   24150.50000000  .00020000  00000-0  28000-4 0  9999",
    "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 229.5775 15.509955193 12345",
}


@pytest.fixture
def runtime_topology():
    """Return a manager whose state exists only at runtime (no SimulationProperty)."""
    project = create_empty_project(
        simulation_name="RuntimeRoundTrip",
        start_date=START_DATE.strftime("%d/%m/%Y %H:%M:%S"),
        end_date=END_DATE.strftime("%d/%m/%Y %H:%M:%S"),
    )
    sim_manager = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )
    create_custom_satellite(_CUSTOM_SATELLITE, sim_manager, object_id=7)
    ground_station = create_custom_ground_station(
        "AuditGS", 48.0, 2.0, 0.0, object_id=9
    )
    add_custom_ground_station(ground_station, sim_manager)
    return TopologyManager.from_simulation_manager(sim_manager)


def test_runtime_state_roundtrip_preserves_dates_and_nodes(tmp_path, runtime_topology):
    assert runtime_topology.simulation_property is None
    path = tmp_path / "runtime_topology.json"

    runtime_topology.to_file(str(path))
    reloaded = TopologyManager.from_file(str(path))

    assert reloaded.simulation_manager.time_manager.start_date == START_DATE
    assert reloaded.simulation_manager.time_manager.end_date == END_DATE
    assert [
        sat.satellite_name for sat in reloaded.simulation_manager.get_satellites()
    ] == ["AuditSat-001"]
    assert [gs.label for gs in reloaded.simulation_manager.get_ground_stations()] == [
        "AuditGS"
    ]


def _write_payload(path: Path, basename: object, content: str = _BENIGN_CSV) -> None:
    """Write a topology payload whose embedded ground file has *basename*."""
    payload = json.loads(Path(TEST_TOPOLOGY).read_text())
    payload["ground_files"] = {
        _ORIGINAL_DATA_FILE: {"content": content, "basename": basename}
    }
    path.write_text(json.dumps(payload))


class TestGroundFileRestorePaths:
    """Embedded ground station files must only be restored inside a fresh dir."""

    def test_rejects_absolute_basename_and_writes_nothing(self, tmp_path):
        escape_target = tmp_path / "absolute_escape.csv"
        payload_path = tmp_path / "topology.json"
        _write_payload(payload_path, str(escape_target))

        with pytest.raises(ValueError, match="ground file basename"):
            TopologyManager.from_file(str(payload_path))

        assert not escape_target.exists()

    @pytest.mark.parametrize(
        "basename", ["../escaped.csv", "sub/escaped.csv", "", ".", ".."]
    )
    def test_rejects_non_plain_basenames_and_writes_nothing(self, tmp_path, basename):
        payload_path = tmp_path / "topology.json"
        _write_payload(payload_path, basename)

        with pytest.raises(ValueError, match="ground file basename"):
            TopologyManager.from_file(str(payload_path))

        assert not (tmp_path / "escaped.csv").exists()
        assert not (tmp_path / "sub" / "escaped.csv").exists()

    def test_benign_ground_files_roundtrip(self, tmp_path):
        topology = TopologyManager.from_file(TEST_TOPOLOGY)
        payload_path = tmp_path / "topology.json"

        topology.to_file(str(payload_path))
        reloaded = TopologyManager.from_file(str(payload_path))

        assert [
            gs.label for gs in reloaded.simulation_manager.get_ground_stations()
        ] == ["Berlin", "London", "Paris", "Rome", "Madrid"]
        data_file = reloaded.simulation_property.ground_objects_properties[0].data_file
        assert os.path.isfile(data_file)
        assert Path(data_file).name == "ground_stations.txt"

    def test_same_stem_loads_use_distinct_restore_directories(self, tmp_path):
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        _write_payload(first_dir / "topology.json", "ground_stations.txt", _BENIGN_CSV)
        _write_payload(second_dir / "topology.json", "ground_stations.txt", _OTHER_CSV)

        first = TopologyManager.from_file(str(first_dir / "topology.json"))
        second = TopologyManager.from_file(str(second_dir / "topology.json"))

        first_data_file = Path(
            first.simulation_property.ground_objects_properties[0].data_file
        )
        second_data_file = Path(
            second.simulation_property.ground_objects_properties[0].data_file
        )
        assert first_data_file.parent != second_data_file.parent
        assert first_data_file.read_text() == _BENIGN_CSV
        assert second_data_file.read_text() == _OTHER_CSV
