"""Direct contract tests for the extracted topology persistence codec."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from sat_com_application.simulation_manager import SimulationManager

from satgonetem.services.topology_persistence import (
    ground_file_restore_path,
    restore_runtime_state,
    restore_simulation_property,
    save_to_file,
    serialize_runtime_state,
)
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
RUNTIME_DATE_FORMAT = "%d/%m/%Y %H:%M:%S"


@pytest.fixture(scope="module")
def topology():
    return TopologyManager.from_file(TEST_TOPOLOGY)


@pytest.fixture(scope="module")
def runtime_state():
    project = create_empty_project(
        simulation_name="CodecProject",
        start_date="01/01/2024 00:00:00",
        end_date="01/01/2024 01:00:00",
    )
    sim_manager = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )
    create_custom_satellite(
        {
            "name": "CodecSat",
            "tle_line1": "1 25544U 98067A   24150.50000000  .00020000  00000-0  28000-4 0  9999",
            "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 229.5775 15.509955193 12345",
        },
        sim_manager,
        object_id=3,
    )
    add_custom_ground_station(
        create_custom_ground_station("CodecGS", 10.0, 20.0, 0.0, object_id=4),
        sim_manager,
    )
    return serialize_runtime_state(TopologyManager.from_simulation_manager(sim_manager))


class TestGroundFileRestorePath:
    def test_accepts_a_plain_name_inside_the_directory(self, tmp_path):
        restored = ground_file_restore_path(str(tmp_path), "stations.csv")

        assert Path(restored).parent == tmp_path.resolve()
        assert Path(restored).name == "stations.csv"

    @pytest.mark.parametrize(
        "basename", ["", ".", "..", "/tmp/escape.csv", "sub/escape.csv", None, 1]
    )
    def test_rejects_unsafe_names(self, tmp_path, basename):
        with pytest.raises(ValueError, match="ground file basename"):
            ground_file_restore_path(str(tmp_path), basename)


class TestSerializeRuntimeState:
    def test_reads_nodes_and_links_from_the_simulation_manager(self, runtime_state):
        assert runtime_state["satellites"][0]["name"] == "CodecSat"
        assert runtime_state["ground_stations"][0]["label"] == "CodecGS"
        assert runtime_state["links"]["inter_satellite"] == []
        assert datetime.strptime(
            runtime_state["start_date"], RUNTIME_DATE_FORMAT
        ) == datetime(2024, 1, 1, 0, 0, 0)


class TestSaveToFile:
    def test_writes_the_default_instance_config(self, tmp_path, topology):
        path = tmp_path / "saved.json"

        save_to_file(topology, str(path))
        payload = json.loads(path.read_text())

        assert (
            payload["network_config"]
            == topology._network_config_from_instance().__dict__
        )

    def test_writes_an_explicit_config_wholesale(self, tmp_path, topology):
        path = tmp_path / "saved.json"

        save_to_file(
            topology,
            str(path),
            NetworkConfig(link_netem_limit=2048, link_txqueuelen=4096),
        )
        payload = json.loads(path.read_text())

        assert payload["network_config"]["link_netem_limit"] == 2048
        assert payload["network_config"]["link_txqueuelen"] == 4096
        assert "runtime_state" not in payload


class TestRestore:
    def test_restore_simulation_property_rewrites_ground_files(self, tmp_path):
        payload = json.loads(Path(TEST_TOPOLOGY).read_text())

        simulation_property = restore_simulation_property(
            payload, str(tmp_path / "topology.json")
        )

        data_file = simulation_property.ground_objects_properties[0].data_file
        assert Path(data_file).name == "ground_stations.txt"
        assert Path(data_file).exists()
        assert Path(data_file).parent.name.startswith("topology_ground_files_")

    def test_restore_runtime_state_skips_malformed_node_records(
        self, runtime_state, caplog
    ):
        import logging

        state = dict(runtime_state)
        state["satellites"] = [{"name": "BadSat"}]
        state["ground_stations"] = [{"label": "NoPosition"}]
        state["user_terminals"] = [{"label": "NoUTPosition"}]
        state["points_of_presence"] = [{"label": "NoPoPPosition"}]
        state["links"] = {}

        with caplog.at_level(logging.WARNING):
            sim_manager = restore_runtime_state(state, NetworkConfig())

        assert sim_manager.get_satellites() == []
        assert sim_manager.get_ground_stations() == []
        assert "Failed to restore" in caplog.text

    def test_restore_runtime_state_returns_a_loaded_simulation_manager(
        self, runtime_state
    ):
        sim_manager = restore_runtime_state(runtime_state, NetworkConfig())

        assert isinstance(sim_manager, SimulationManager)
        assert [sat.satellite_name for sat in sim_manager.get_satellites()] == [
            "CodecSat"
        ]
        assert [gs.label for gs in sim_manager.get_ground_stations()] == ["CodecGS"]
        assert sim_manager.time_manager.start_date == datetime(2024, 1, 1, 0, 0, 0)


class TestPersistenceBugFixes:
    def test_apply_runtime_state_builds_interfaces_when_link_becomes_active(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from satgonetem.models.interface import Interface
        from satgonetem.services.topology_persistence import apply_runtime_state
        from tests.support.factories import make_link, make_satellite

        sat0 = make_satellite("Sat0")
        sat2 = make_satellite("Sat2")
        link_to_enable = make_link(sat0, sat2)
        link_to_enable.is_active = False
        link_to_enable.satcom_object = SimpleNamespace(
            source=SimpleNamespace(satellite_name="S0"),
            destination=SimpleNamespace(satellite_name="S2"),
            disabled=True,
        )
        link_to_enable.peer_interfaces = []

        built_calls = []

        def fake_build(lnk, set_ip=True, sync_to_node=False):
            ia = Interface(f"{lnk.source.name}.{lnk.target.id}")
            ib = Interface(f"{lnk.target.name}.{lnk.source.id}")
            lnk.peer_interfaces = [ia, ib]
            lnk.source.interfaces.append(ia)
            lnk.target.interfaces.append(ib)
            manager.interfaces.extend([ia, ib])
            built_calls.append((lnk, set_ip, sync_to_node))

        manager = SimpleNamespace(
            links={frozenset(("Sat0", "Sat2")): link_to_enable},
            interfaces=[],
            interface_version=0,
            _build_interfaces_from_link=MagicMock(side_effect=fake_build),
        )

        runtime_state = {
            "links": {
                "inter_satellite": [
                    {
                        "source": "S0",
                        "destination": "S2",
                        "disabled": False,
                        "is_active": True,
                    },
                ]
            }
        }
        apply_runtime_state(manager, runtime_state)

        assert link_to_enable.is_active is True
        assert len(link_to_enable.peer_interfaces) == 2
        assert built_calls == [(link_to_enable, True, False)]

    def test_restore_runtime_state_handles_reversed_gsl_and_utl_endpoints(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from satgonetem.services.network_config import NetworkConfig
        from satgonetem.services.topology_persistence import restore_runtime_state

        sat = SimpleNamespace(satellite_name="SAT-0")
        gs = SimpleNamespace(label="GS-0")
        ut = SimpleNamespace(label="UT-0")

        sim_manager = MagicMock()
        sim_manager.get_satellites.return_value = [sat]
        sim_manager.get_ground_stations.return_value = [gs]
        sim_manager.get_user_terminals.return_value = [ut]
        sim_manager.get_all_ground_objects.return_value = [gs, ut]
        sim_manager.get_inter_satellite_links.return_value = []
        sim_manager.get_ground_station_links.return_value = []
        sim_manager.get_user_terminal_links.return_value = []
        sim_manager.get_ground_object_links.return_value = []

        runtime_state = {
            "simulation_name": "ReversedLinks",
            "links": {
                "ground_station": [{"source": "GS-0", "destination": "SAT-0"}],
                "user_terminal": [{"source": "UT-0", "destination": "SAT-0"}],
            },
        }
        with patch(
            "satgonetem.services.topology_persistence.create_and_load_simulation",
            return_value=sim_manager,
        ):
            restore_runtime_state(runtime_state, NetworkConfig())

        sim_manager.create_and_add_ground_station_link_connection.assert_called_once_with(
            sat, gs
        )
        sim_manager.create_and_add_user_terminal_link_connection.assert_called_once_with(
            sat, ut
        )
