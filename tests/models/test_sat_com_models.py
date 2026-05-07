"""Tests for the Pydantic configuration models in sat_com_models."""

import pytest
from pydantic import ValidationError

from satgonetem.models.sat_com_models import (
    Configuration,
    ConnectivityProperties,
    ConstellationProperty,
    CustomSatellite,
    GroundObject,
    MPLSConfiguration,
    NetworkProperties,
    OrbitalConnectivityProperty,
    WalkerShell,
)


CONSTELLATION_DATA = {
    "identifier": "iris2",
    "amount_of_orbit_plane": 6,
    "amount_of_satellite_per_orbit_plane": 10,
    "inclination": 53.0,
    "phase_difference_between_satellites": True,
    "mean_revolution_per_day": 14.5,
}

ORBITAL_CONNECTIVITY_DATA = {
    "adjacent_inter_satellite_shifting": 1,
    "maximum_inter_satellite_count": 4,
    "maximum_inter_satellite_range_distance": 2000.0,
    "maximum_ground_station_range": 1500.0,
    "maximum_user_terminal_range": 1200.0,
    "maximum_connected_ground_object": 5,
    "maximum_connected_user_terminal": 3,
    "maximum_connected_ground_station": 2,
}

CONNECTIVITY_PROPS_DATA = {
    "ground_to_space_connections_strategy": "nearest",
    "elevation_above_horizon": 10,
    "maximum_satellite_range_distance": 1500.0,
    "shell_white_lists": ["iris2"],
    "maximum_connected_satellites": 2,
}


class TestConstellationProperty:
    """Tests for ConstellationProperty Pydantic model."""

    def test_valid_creation(self):
        prop = ConstellationProperty(**CONSTELLATION_DATA)
        assert prop.identifier == "iris2"
        assert prop.amount_of_orbit_plane == 6
        assert prop.inclination == 53.0
        assert prop.phase_difference_between_satellites is True

    def test_missing_required_field_raises(self):
        data = {k: v for k, v in CONSTELLATION_DATA.items() if k != "identifier"}
        with pytest.raises(ValidationError):
            ConstellationProperty(**data)


class TestOrbitalConnectivityProperty:
    """Tests for OrbitalConnectivityProperty Pydantic model."""

    def test_valid_creation(self):
        prop = OrbitalConnectivityProperty(**ORBITAL_CONNECTIVITY_DATA)
        assert prop.adjacent_inter_satellite_shifting == 1
        assert prop.maximum_inter_satellite_count == 4
        assert prop.maximum_ground_station_range == 1500.0

    def test_missing_required_field_raises(self):
        data = {k: v for k, v in ORBITAL_CONNECTIVITY_DATA.items()
                if k != "maximum_ground_station_range"}
        with pytest.raises(ValidationError):
            OrbitalConnectivityProperty(**data)


class TestWalkerShell:
    """Tests for WalkerShell Pydantic model."""

    def test_valid_delta_type(self):
        shell = WalkerShell(
            type="delta",
            constellation_property=ConstellationProperty(**CONSTELLATION_DATA),
            orbital_connectivity_property=OrbitalConnectivityProperty(**ORBITAL_CONNECTIVITY_DATA),
            ground_object_white_list=["gs1", "gs2"],
        )
        assert shell.type == "delta"
        assert len(shell.ground_object_white_list) == 2

    def test_valid_star_type(self):
        shell = WalkerShell(
            type="star",
            constellation_property=ConstellationProperty(**CONSTELLATION_DATA),
            orbital_connectivity_property=OrbitalConnectivityProperty(**ORBITAL_CONNECTIVITY_DATA),
            ground_object_white_list=[],
        )
        assert shell.type == "star"

    def test_invalid_type_raises(self):
        with pytest.raises(ValidationError):
            WalkerShell(
                type="polar",
                constellation_property=ConstellationProperty(**CONSTELLATION_DATA),
                orbital_connectivity_property=OrbitalConnectivityProperty(**ORBITAL_CONNECTIVITY_DATA),
                ground_object_white_list=[],
            )


class TestConnectivityProperties:
    """Tests for ConnectivityProperties Pydantic model."""

    def test_valid_creation(self):
        props = ConnectivityProperties(**CONNECTIVITY_PROPS_DATA)
        assert props.ground_to_space_connections_strategy == "nearest"
        assert props.elevation_above_horizon == 10
        assert props.shell_white_lists == ["iris2"]
        assert props.maximum_connected_satellites == 2

    def test_missing_field_raises(self):
        data = {k: v for k, v in CONNECTIVITY_PROPS_DATA.items()
                if k != "elevation_above_horizon"}
        with pytest.raises(ValidationError):
            ConnectivityProperties(**data)


class TestGroundObject:
    """Tests for GroundObject Pydantic model."""

    def test_valid_ground_station(self):
        obj = GroundObject(
            identifier="gs1",
            data_file="ground_stations.txt",
            type="ground_station",
            connectivity_properties=ConnectivityProperties(**CONNECTIVITY_PROPS_DATA),
        )
        assert obj.identifier == "gs1"
        assert obj.type == "ground_station"

    def test_valid_user_terminal(self):
        obj = GroundObject(
            identifier="ut1",
            data_file="user_terminals.txt",
            type="user_terminal",
            connectivity_properties=ConnectivityProperties(**CONNECTIVITY_PROPS_DATA),
        )
        assert obj.type == "user_terminal"

    def test_invalid_type_raises(self):
        with pytest.raises(ValidationError):
            GroundObject(
                identifier="x",
                data_file="file.txt",
                type="relay_station",
                connectivity_properties=ConnectivityProperties(**CONNECTIVITY_PROPS_DATA),
            )


class TestCustomSatellite:
    """Tests for CustomSatellite Pydantic model."""

    def test_valid_creation(self):
        sat = CustomSatellite(
            name="ISS",
            tle_line1="1 25544U 98067A   21001.00000000  .00001000  00000-0  10000-4 0  9990",
            tle_line2="2 25544  51.6461 224.0672 0001291  84.3958 275.7368 15.49310068261382",
        )
        assert sat.name == "ISS"
        assert sat.tle_line1.startswith("1 25544")

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            CustomSatellite(
                tle_line1="1 25544...",
                tle_line2="2 25544...",
            )


class TestMPLSConfiguration:
    """Tests for MPLSConfiguration Pydantic model."""

    def test_default_values(self):
        config = MPLSConfiguration()
        assert config.label_range_start == 16
        assert config.label_range_end == 1048575
        assert config.use_ldp is False
        assert config.php is True
        assert config.ttl == 64

    def test_custom_values(self):
        config = MPLSConfiguration(label_range_start=100, use_ldp=True, ttl=32)
        assert config.label_range_start == 100
        assert config.use_ldp is True
        assert config.ttl == 32


class TestNetworkProperties:
    """Tests for NetworkProperties Pydantic model."""

    def test_valid_static_routing(self):
        props = NetworkProperties(
            inter_satellite_link_capacity=1000,
            ground_station_link_capacity=500,
            routing_method="Static",
            protocol="ipv4",
        )
        assert props.routing_method == "Static"
        assert props.mpls_config is None

    def test_with_optional_mpls_config(self):
        props = NetworkProperties(
            inter_satellite_link_capacity=1000,
            ground_station_link_capacity=500,
            routing_method="MPLS",
            protocol="ipv4",
            mpls_config=MPLSConfiguration(),
        )
        assert props.mpls_config is not None
        assert props.mpls_config.ttl == 64


class TestConfiguration:
    """Tests for the top-level Configuration Pydantic model."""

    def _build(self, **overrides):
        walker_shell = WalkerShell(
            type="delta",
            constellation_property=ConstellationProperty(**CONSTELLATION_DATA),
            orbital_connectivity_property=OrbitalConnectivityProperty(**ORBITAL_CONNECTIVITY_DATA),
            ground_object_white_list=[],
        )
        ground_object = GroundObject(
            identifier="gs1",
            data_file="gs.txt",
            type="ground_station",
            connectivity_properties=ConnectivityProperties(**CONNECTIVITY_PROPS_DATA),
        )
        base = dict(
            simulation_name="test_sim",
            start_date="2024-01-01T00:00:00",
            end_date="2024-01-01T01:00:00",
            movement_model="SGP4",
            distance_model="Cartesian",
            ground_objects_properties=[ground_object],
            walker_shells=[walker_shell],
            disable_ground_station_link_preload=False,
            static_ground_station_link_mode=True,
        )
        base.update(overrides)
        return Configuration(**base)

    def test_valid_configuration(self):
        config = self._build()
        assert config.simulation_name == "test_sim"
        assert len(config.walker_shells) == 1
        assert len(config.ground_objects_properties) == 1

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            Configuration(
                simulation_name="test",
                start_date="2024-01-01",
            )
