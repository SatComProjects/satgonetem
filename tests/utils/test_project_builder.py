"""Tests for satgonetem.utils.project_builder."""

import pathlib
import pytest

from sat_com_builder.models import GroundConnectivityProperty

from satgonetem.utils.project_builder import (
    GroundObject,
    GroundObjectFile,
    GroundStationEntry,
    create_test_project,
    create_empty_project,
    create_and_load_simulation,
    create_custom_satellite,
    create_custom_ground_station,
    add_custom_ground_station,
    _find_satellite_by_name,
    _find_ground_station_by_name,
    _find_ground_object_by_name,
)

CONN_PROPS = GroundConnectivityProperty(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=10,
    maximum_satellite_range_distance=1500.0,
    shells_whitelist=["LEO"],
    maximum_connected_satellites=3,
)


class TestGroundStationEntry:
    """Tests for GroundStationEntry."""

    def test_to_csv_line(self):
        entry = GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034)
        assert entry.to_csv_line() == "0,Berlin,52.52,13.405,34"

    def test_to_csv_line_negative_coordinates(self):
        entry = GroundStationEntry(1, "Punta Arenas", -53.1638, -70.9171, 0.034)
        assert entry.to_csv_line() == "1,Punta Arenas,-53.1638,-70.9171,34"


class TestGroundObjectFile:
    """Tests for GroundObjectFile."""

    def _make_file(self) -> GroundObjectFile:
        entries = [
            GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
            GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
        ]
        return GroundObjectFile("Ground Stations", entries)

    def test_write_creates_file(self, tmp_path):
        gof = self._make_file()
        path = gof.write(str(tmp_path))
        assert pathlib.Path(path).exists()

    def test_write_filename_derived_from_identifier(self, tmp_path):
        gof = self._make_file()
        path = gof.write(str(tmp_path))
        assert pathlib.Path(path).name == "ground_stations.txt"

    def test_write_file_contents(self, tmp_path):
        gof = self._make_file()
        path = gof.write(str(tmp_path))
        lines = pathlib.Path(path).read_text().splitlines()
        assert lines[0] == "0,Berlin,52.52,13.405,34"
        assert lines[1] == "1,London,51.507,-0.127,11"

    def test_write_returns_absolute_path(self, tmp_path):
        gof = self._make_file()
        path = gof.write(str(tmp_path))
        assert pathlib.Path(path).is_absolute()

    def test_write_empty_entries(self, tmp_path):
        gof = GroundObjectFile("Empty Group", [])
        path = gof.write(str(tmp_path))
        assert pathlib.Path(path).read_text() == ""

    def test_from_csv_converts_metres_to_km(self, tmp_path):
        path = tmp_path / "ground_stations.txt"
        path.write_text("0,Berlin,52.52,13.405,34\n")

        gof = GroundObjectFile.from_csv("Ground Stations", str(path))

        assert gof.entries[0].elevation_km == pytest.approx(0.034)


class TestGroundObject:
    """Tests for the GroundObject builder."""

    def _make_ground_object(self) -> GroundObject:
        entries = [GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034)]
        gs_file = GroundObjectFile("Ground Stations", entries)
        return GroundObject(gs_file, "ground_station", CONN_PROPS)

    def test_to_dict_keys(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path))
        assert set(result.keys()) == {
            "identifier",
            "data_file",
            "type",
            "connectivity_properties",
        }

    def test_to_dict_identifier(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path))
        assert result["identifier"] == "Ground Stations"

    def test_to_dict_type(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path))
        assert result["type"] == "ground_station"

    def test_to_dict_data_file_written(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path))
        assert pathlib.Path(result["data_file"]).exists()

    def test_to_dict_connectivity_properties_dict(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path))
        conn = result["connectivity_properties"]
        assert conn["elevation_above_horizon"] == 10
        assert conn["maximum_connected_satellites"] == 3
        assert conn["shells_whitelist"] == ["LEO"]

    def test_to_dict_inline_objects(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path), inline=True)

        assert "data_file" not in result
        assert result["ground_objects"] == [
            {
                "id": 0,
                "label": "Berlin",
                "latitude": 52.52,
                "longitude": 13.405,
                "altitude": 34.0,
            }
        ]

    def test_to_dict_user_terminal_type(self, tmp_path):
        entries = [GroundStationEntry(0, "Terminal A", 10.0, 20.0, 0.01)]
        gs_file = GroundObjectFile("User Terminals", entries)
        go = GroundObject(gs_file, "user_terminal", CONN_PROPS)
        result = go.to_dict(str(tmp_path))
        assert result["type"] == "user_terminal"


class TestCreateCustomGroundStation:
    """Tests for create_custom_ground_station."""

    def test_ground_station_has_correct_label(self):
        gs = create_custom_ground_station("Berlin", 52.52, 13.405, 0.034)
        assert gs.label == "Berlin"

    def test_ground_station_position_is_set(self):
        gs = create_custom_ground_station("Tokyo", 35.68, 139.69, 0.04)
        pos = gs.get_coordinates()
        assert pos.latitude == pytest.approx(35.68)
        assert pos.longitude == pytest.approx(139.69)
        assert pos.altitude == pytest.approx(40.0)

    def test_ground_station_default_object_id_and_domain(self):
        gs = create_custom_ground_station("Test", 0.0, 0.0, 0.0)
        assert gs.object_id == 0
        assert gs.domain == "public"

    def test_ground_station_custom_object_id_and_domain(self):
        gs = create_custom_ground_station(
            "Test", 0.0, 0.0, 0.0, object_id=42, domain="private"
        )
        assert gs.object_id == 42
        assert gs.domain == "private"


class TestSimulationDates:
    """Tests for default start/end date handling in project builders."""

    def test_test_project_end_date_is_start_plus_ten_minutes(self):
        project = create_test_project(start_date="01/01/2024 00:00:00")

        assert project.start_date == "01/01/2024 00:00:00"
        assert project.end_date == "01/01/2024 00:10:00"

    def test_test_project_future_start_date_keeps_end_after_start(self):
        project = create_test_project(start_date="01/01/2030 12:00:00")

        assert project.start_date == "01/01/2030 12:00:00"
        assert project.end_date == "01/01/2030 12:10:00"

    def test_test_project_explicit_end_date_is_preserved(self):
        project = create_test_project(
            start_date="01/01/2024 00:00:00",
            end_date="01/01/2024 02:00:00",
        )

        assert project.end_date == "01/01/2024 02:00:00"

    def test_test_project_uses_inline_ground_objects(self):
        project = create_test_project(start_date="01/01/2024 00:00:00")

        ground_object = project.ground_objects_properties[0]
        assert ground_object.data_file is None
        assert ground_object.ground_objects[0].altitude == pytest.approx(34.0)
        assert ground_object.connectivity_properties.shells_whitelist == ["LEO"]

        orbital = project.walker_shells[0].orbital_connectivity_property
        assert "maximum_inter_satellite_count" not in orbital.model_dump()

    def test_empty_project_end_date_is_start_plus_ten_minutes(self):
        project = create_empty_project(start_date="02/02/2025 06:30:00")

        assert project.start_date == "02/02/2025 06:30:00"
        assert project.end_date == "02/02/2025 06:40:00"


class TestSimulationManagerHelpers:
    """Tests for helper functions that interact with a SimulationManager."""

    @pytest.fixture(scope="class")
    def sim_manager(self):
        project = create_test_project()
        return create_and_load_simulation(project.model_dump(), project.simulation_name)

    def test_create_custom_satellite_adds_to_manager(self, sim_manager):
        before = len(sim_manager.get_satellites())
        custom_sat = {
            "name": "TestSat-001",
            "tle_line1": "1 25544U 98067A   24150.50000000  .00020000  00000-0  28000-4 0  9999",
            "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 229.5775 15.509955193 12345",
        }
        new_id = create_custom_satellite(custom_sat, sim_manager)
        after = len(sim_manager.get_satellites())
        assert after == before + 1
        assert isinstance(new_id, int)
        assert new_id in {sat.topology_uniq_id for sat in sim_manager.get_satellites()}

    def test_add_custom_ground_station_adds_to_manager(self, sim_manager):
        before = len(sim_manager.get_ground_stations())
        gs = create_custom_ground_station("TestGS", 0.0, 0.0, 0.0)
        add_custom_ground_station(gs, sim_manager)
        after = len(sim_manager.get_ground_stations())
        assert after == before + 1

    def test_add_custom_ground_station_attaches_existing_domain(self, sim_manager):
        gs = create_custom_ground_station(
            "DomainTest",
            48.0,
            2.0,
            0.0,
            domain="Ground Stations",
        )
        add_custom_ground_station(gs, sim_manager)

        assert gs.ground_object_domain is not None
        assert gs.ground_object_domain.identifier == "Ground Stations"
        assert gs.ground_object_domain.type == "ground_station"

    def test_add_custom_ground_station_supports_link_updates(self, sim_manager):
        gs = create_custom_ground_station("LinkUpdateTest", 48.0, 2.0, 0.0)
        add_custom_ground_station(gs, sim_manager)

        # The documented flow calls this immediately after adding the station.
        sim_manager.update_ground_station_links()

        assert gs.ground_object_domain is not None

    def test_find_satellite_by_name(self, sim_manager):
        sat = _find_satellite_by_name("LEO 0", sim_manager)
        assert sat.satellite_name == "LEO 0"

    def test_find_satellite_by_name_not_found(self, sim_manager):
        with pytest.raises(
            ValueError, match="Satellite with name 'MissingSat' not found"
        ):
            _find_satellite_by_name("MissingSat", sim_manager)

    def test_find_ground_station_by_name(self, sim_manager):
        gs = _find_ground_station_by_name("Berlin", sim_manager)
        assert gs.label == "Berlin"

    def test_find_ground_station_by_name_not_found(self, sim_manager):
        with pytest.raises(
            ValueError, match="Ground station with name 'MissingGS' not found"
        ):
            _find_ground_station_by_name("MissingGS", sim_manager)

    def test_find_ground_object_by_name(self, sim_manager):
        go = _find_ground_object_by_name("Berlin", sim_manager)
        assert go.label == "Berlin"

    def test_find_ground_object_by_name_not_found(self, sim_manager):
        with pytest.raises(
            ValueError, match="Ground object with name 'MissingGO' not found"
        ):
            _find_ground_object_by_name("MissingGO", sim_manager)
