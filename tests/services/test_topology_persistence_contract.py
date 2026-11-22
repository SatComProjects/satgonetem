"""Characterization tests for the TopologyManager persistence contract.

Pins the JSON payload shape written by ``to_file`` and the runtime-state
records consumed by ``from_file`` so the codec can be extracted without
serialization drift.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
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
RUNTIME_DATE_FORMAT = "%d/%m/%Y %H:%M:%S"

_CUSTOM_SATELLITE = {
    "name": "ShapeSat-001",
    "tle_line1": "1 25544U 98067A   24150.50000000  .00020000  00000-0  28000-4 0  9999",
    "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 229.5775 15.509955193 12345",
}

_RUNTIME_STATE_KEYS = {
    "simulation_name",
    "start_date",
    "end_date",
    "satellites",
    "ground_stations",
    "user_terminals",
    "points_of_presence",
    "links",
}

_LINK_STATE_KEYS = {
    "inter_satellite",
    "ground_station",
    "user_terminal",
    "ground_object",
}

_SATELLITE_RECORD_KEYS = {
    "name",
    "tle_line1",
    "tle_line2",
    "object_id",
    "domain",
    "addressable",
}

_GROUND_OBJECT_RECORD_KEYS = {
    "label",
    "latitude",
    "longitude",
    "altitude",
    "object_id",
    "domain",
}


@pytest.fixture(scope="module")
def runtime_topology():
    project = create_empty_project(
        simulation_name="ShapeProject",
        start_date=START_DATE.strftime(RUNTIME_DATE_FORMAT),
        end_date=END_DATE.strftime(RUNTIME_DATE_FORMAT),
    )
    sim_manager = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )
    create_custom_satellite(_CUSTOM_SATELLITE, sim_manager, object_id=7)
    ground_station = create_custom_ground_station(
        "ShapeGS", 48.0, 2.0, 0.0, object_id=9
    )
    add_custom_ground_station(ground_station, sim_manager)
    return TopologyManager.from_simulation_manager(sim_manager)


class TestRuntimeStateShape:
    def test_top_level_keys_and_date_format(self, runtime_topology):
        state = runtime_topology._serialize_runtime_state()

        assert set(state) == _RUNTIME_STATE_KEYS
        assert set(state["links"]) == _LINK_STATE_KEYS
        # Dates use the format accepted by create_empty_project() on restore.
        assert datetime.strptime(state["start_date"], RUNTIME_DATE_FORMAT) == START_DATE
        assert datetime.strptime(state["end_date"], RUNTIME_DATE_FORMAT) == END_DATE
        # The simulation manager falls back to its default project name.
        assert state["simulation_name"] == "Constellation"

    def test_node_records_keep_their_keys(self, runtime_topology):
        state = runtime_topology._serialize_runtime_state()

        assert [set(record) for record in state["satellites"]] == [
            _SATELLITE_RECORD_KEYS
        ]
        assert [set(record) for record in state["ground_stations"]] == [
            _GROUND_OBJECT_RECORD_KEYS
        ]
        assert state["satellites"][0]["name"] == "ShapeSat-001"
        assert state["satellites"][0]["object_id"] == 7
        assert state["ground_stations"][0]["label"] == "ShapeGS"
        assert state["ground_stations"][0]["object_id"] == 9

    def test_empty_link_lists_are_serialized(self, runtime_topology):
        state = runtime_topology._serialize_runtime_state()

        assert state["links"] == {
            "inter_satellite": [],
            "ground_station": [],
            "user_terminal": [],
            "ground_object": [],
        }


class TestPayloadKeys:
    def test_simulation_property_payload_adds_only_a_runtime_overlay(self, tmp_path):
        topology = TopologyManager.from_file(TEST_TOPOLOGY)
        path = tmp_path / "project.json"

        topology.to_file(str(path))
        payload = json.loads(path.read_text())

        assert set(payload) == {
            "simulation_property",
            "network_config",
            "ground_files",
            "runtime_overlay",
        }
        assert "runtime_state" not in payload
        assert isinstance(payload["simulation_property"], dict)
        assert isinstance(payload["ground_files"], dict)

    def test_programmatic_payload_adds_runtime_state(self, tmp_path, runtime_topology):
        path = tmp_path / "runtime.json"

        runtime_topology.to_file(str(path))
        payload = json.loads(path.read_text())

        assert set(payload) == {
            "simulation_property",
            "network_config",
            "ground_files",
            "runtime_state",
        }
        assert set(payload["runtime_state"]) == _RUNTIME_STATE_KEYS

    def test_network_config_payload_roundtrips_dataclass_fields(
        self, tmp_path, runtime_topology
    ):
        path = tmp_path / "network.json"

        runtime_topology.to_file(str(path))
        payload = json.loads(path.read_text())

        persisted = payload["network_config"]
        assert set(persisted) == set(NetworkConfig().__dict__)
        assert persisted == runtime_topology._network_config_from_instance().__dict__


class TestTolerantRestore:
    def test_one_malformed_link_is_skipped(self, tmp_path, runtime_topology):
        """A single bad link record must warn and skip, not abort the restore."""
        sim_manager = runtime_topology.simulation_manager
        satellite = sim_manager.get_satellites()[0]
        ground_station = sim_manager.get_ground_stations()[0]
        sim_manager.create_and_add_ground_station_link_connection(
            satellite, ground_station
        )

        path = tmp_path / "malformed.json"
        runtime_topology.to_file(str(path))
        payload = json.loads(path.read_text())
        links = payload["runtime_state"]["links"]

        assert links["ground_station"] == [
            {
                "source": "ShapeSat-001",
                "destination": "ShapeGS",
                "loss": 0.0,
                "jitter": 0,
                "admin_up": True,
                "use_budget": False,
                "disabled": False,
                "is_active": True,
            }
        ]
        links["inter_satellite"].append(
            {"source": "MissingSat", "destination": "ShapeSat-001"}
        )
        links["ground_station"].append(
            {"source": "ShapeSat-001", "destination": "MissingGS"}
        )
        links["user_terminal"].append(
            {"source": "ShapeSat-001", "destination": "MissingUT"}
        )
        links["ground_object"].append(
            {"source": "MissingGoA", "destination": "ShapeGS"}
        )
        path.write_text(json.dumps(payload))

        reloaded = TopologyManager.from_file(str(path))

        assert [
            (link.source.satellite_name, link.destination.label)
            for link in reloaded.simulation_manager.get_ground_stations_links()
        ] == [("ShapeSat-001", "ShapeGS")]
        assert reloaded.simulation_manager.get_inter_satellites_links() == []
