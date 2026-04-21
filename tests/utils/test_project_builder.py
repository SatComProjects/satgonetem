"""Tests for satgonetem.utils.project_builder."""

import pathlib
import pytest

from satgonetem.models.sat_com_models import (
    ConnectivityProperties,
    ConstellationProperty,
    OrbitalConnectivityProperty,
    WalkerShell,
)
from satgonetem.utils.project_builder import (
    GroundObject,
    GroundObjectFile,
    GroundStationEntry,
    SatcomProject,
)


CONN_PROPS = ConnectivityProperties(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=10,
    maximum_satellite_range_distance=1500.0,
    shell_white_lists=["LEO"],
    maximum_connected_satellites=3,
)

CONSTELLATION = ConstellationProperty(
    identifier="LEO",
    amount_of_orbit_plane=7,
    amount_of_satellite_per_orbit_plane=11,
    inclination=86.4,
    mean_revolution_per_day=14.35,
    phase_difference_between_satellites=True,
)

ORBITAL_CONN = OrbitalConnectivityProperty(
    adjacent_inter_satellite_shifting=0,
    maximum_inter_satellite_count=4,
    maximum_inter_satellite_range_distance=1500.0,
    maximum_ground_station_range=1200.0,
    maximum_user_terminal_range=1000.0,
    maximum_connected_ground_object=10000,
    maximum_connected_user_terminal=500,
    maximum_connected_ground_station=10,
)

WALKER_SHELL = WalkerShell(
    type="star",
    constellation_property=CONSTELLATION,
    orbital_connectivity_property=ORBITAL_CONN,
    ground_object_white_list=["Ground Stations"],
)


class TestGroundStationEntry:
    """Tests for GroundStationEntry."""

    def test_to_csv_line(self):
        entry = GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034)
        assert entry.to_csv_line() == "0,Berlin,52.52,13.405,0.034"

    def test_to_csv_line_negative_coordinates(self):
        entry = GroundStationEntry(1, "Punta Arenas", -53.1638, -70.9171, 0.034)
        assert entry.to_csv_line() == "1,Punta Arenas,-53.1638,-70.9171,0.034"


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
        assert lines[0] == "0,Berlin,52.52,13.405,0.034"
        assert lines[1] == "1,London,51.507,-0.127,0.011"

    def test_write_returns_absolute_path(self, tmp_path):
        gof = self._make_file()
        path = gof.write(str(tmp_path))
        assert pathlib.Path(path).is_absolute()

    def test_write_empty_entries(self, tmp_path):
        gof = GroundObjectFile("Empty Group", [])
        path = gof.write(str(tmp_path))
        assert pathlib.Path(path).read_text() == ""


class TestGroundObject:
    """Tests for the GroundObject builder."""

    def _make_ground_object(self) -> GroundObject:
        entries = [GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034)]
        gs_file = GroundObjectFile("Ground Stations", entries)
        return GroundObject(gs_file, "ground_station", CONN_PROPS)

    def test_to_dict_keys(self, tmp_path):
        go = self._make_ground_object()
        result = go.to_dict(str(tmp_path))
        assert set(result.keys()) == {"identifier", "data_file", "type", "connectivity_properties"}

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
        assert conn["shell_white_lists"] == ["LEO"]

    def test_to_dict_user_terminal_type(self, tmp_path):
        entries = [GroundStationEntry(0, "Terminal A", 10.0, 20.0, 0.01)]
        gs_file = GroundObjectFile("User Terminals", entries)
        go = GroundObject(gs_file, "user_terminal", CONN_PROPS)
        result = go.to_dict(str(tmp_path))
        assert result["type"] == "user_terminal"


class TestSatcomProject:
    """Tests for the SatcomProject top-level builder."""

    def _make_project(self, ground_objects=None) -> SatcomProject:
        if ground_objects is None:
            entries = [GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034)]
            gs_file = GroundObjectFile("Ground Stations", entries)
            ground_objects = [GroundObject(gs_file, "ground_station", CONN_PROPS)]
        return SatcomProject(
            simulation_name="Iridium",
            start_date="01/01/2024 00:00:00",
            end_date="01/01/2024 00:01:00",
            walker_shells=[WALKER_SHELL],
            ground_objects=ground_objects,
        )

    def test_top_level_keys(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        expected_keys = {
            "simulation_name",
            "start_date",
            "end_date",
            "movement_model",
            "distance_model",
            "disable_ground_station_link_preload",
            "static_ground_station_link_mode",
            "ground_objects_properties",
            "walker_shells",
        }
        assert set(result.keys()) == expected_keys

    def test_simulation_name(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        assert result["simulation_name"] == "Iridium"

    def test_dates(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        assert result["start_date"] == "01/01/2024 00:00:00"
        assert result["end_date"] == "01/01/2024 00:01:00"

    def test_defaults(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        assert result["movement_model"] == "pyorbital"
        assert result["distance_model"] == "sklearn"
        assert result["disable_ground_station_link_preload"] is False
        assert result["static_ground_station_link_mode"] is False

    def test_walker_shells_serialized(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        shells = result["walker_shells"]
        assert len(shells) == 1
        assert shells[0]["type"] == "star"
        assert shells[0]["constellation_property"]["identifier"] == "LEO"
        assert shells[0]["ground_object_white_list"] == ["Ground Stations"]

    def test_ground_objects_serialized(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        gos = result["ground_objects_properties"]
        assert len(gos) == 1
        assert gos[0]["identifier"] == "Ground Stations"
        assert gos[0]["type"] == "ground_station"

    def test_ground_files_written_to_specified_dir(self, tmp_path):
        project = self._make_project()
        result = project.to_sat_com_config_dict(str(tmp_path))
        data_file = result["ground_objects_properties"][0]["data_file"]
        assert pathlib.Path(data_file).parent == tmp_path

    def test_multiple_ground_objects(self, tmp_path):
        entries_a = [GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034)]
        entries_b = [GroundStationEntry(0, "Terminal A", 10.0, 20.0, 0.01)]
        gos = [
            GroundObject(GroundObjectFile("Ground Stations", entries_a), "ground_station", CONN_PROPS),
            GroundObject(GroundObjectFile("User Terminals", entries_b), "user_terminal", CONN_PROPS),
        ]
        project = self._make_project(ground_objects=gos)
        result = project.to_sat_com_config_dict(str(tmp_path))
        assert len(result["ground_objects_properties"]) == 2

    def test_custom_movement_and_distance_model(self, tmp_path):
        project = SatcomProject(
            simulation_name="Test",
            start_date="01/01/2024 00:00:00",
            end_date="01/01/2024 00:01:00",
            walker_shells=[WALKER_SHELL],
            ground_objects=[],
            movement_model="sgp4",
            distance_model="cartesian",
        )
        result = project.to_sat_com_config_dict(str(tmp_path))
        assert result["movement_model"] == "sgp4"
        assert result["distance_model"] == "cartesian"
