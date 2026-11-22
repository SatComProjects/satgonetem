"""Tests for the Satellite model."""

import shlex
from unittest.mock import MagicMock

import pytest

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.models.satellite import Satellite


class TestSatelliteInit:
    """Tests for Satellite.__init__."""

    def test_name_and_id(self):
        sat = Satellite("Sat0")
        assert sat.name == "Sat0"
        assert sat.id == 0

    def test_multi_digit_id(self):
        sat = Satellite("Sat42")
        assert sat.id == 42

    def test_qos_flags_initial_state(self):
        sat = Satellite("Sat0")
        assert sat.default_qos_is_on is True
        assert sat.program_specific_qos_is_on is False
        assert sat.qos_configuration_count == 0

    def test_satcom_object_is_none(self):
        sat = Satellite("Sat0")
        assert sat.satcom_object is None

    def test_inherits_from_node(self):
        sat = Satellite("Sat0")
        assert isinstance(sat, Node)

    def test_interfaces_empty(self):
        sat = Satellite("Sat0")
        assert sat.interfaces == []

    def test_routing_tables_empty(self):
        sat = Satellite("Sat0")
        assert sat.ipv4_routing_table == []


class TestSatelliteSyncPositionFromSatcom:
    """Tests for Satellite.sync_position_from_satcom."""

    def test_raises_when_satcom_object_is_none(self):
        sat = Satellite("Sat0")
        with pytest.raises(ValueError, match="Satcom object is not set"):
            sat.sync_position_from_satcom()

    def test_updates_latitude_longitude_altitude(self):
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (45.0, -90.0, 550_000.0)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom()

        assert sat.position["latitude"] == pytest.approx(45.0)
        assert sat.position["longitude"] == pytest.approx(-90.0)
        assert sat.position["altitude"] == pytest.approx(550.0)

    def test_kilometre_input_is_not_scaled(self):
        """An explicit kilometre value is stored as-is."""
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (0.0, 0.0, 0.55)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom(altitude_unit="km")

        assert sat.position["altitude"] == pytest.approx(0.55)

    def test_metre_low_altitude_is_not_mistaken_for_kilometres(self):
        """50 km reported in metres must stay 50 km, not become 50,000 km."""
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (0.0, 0.0, 50_000.0)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom(altitude_unit="m")

        assert sat.position["altitude"] == pytest.approx(50.0)

    def test_metre_altitude_below_100_km_is_converted_correctly(self):
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (0.0, 0.0, 1_000.0)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom()

        assert sat.position["altitude"] == pytest.approx(1.0)

    def test_kilometre_altitude_is_stored_without_conversion(self):
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (10.0, 20.0, 550.0)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom(altitude_unit="km")

        assert sat.position["altitude"] == pytest.approx(550.0)

    def test_invalid_altitude_unit_raises(self):
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (0.0, 0.0, 550_000.0)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        with pytest.raises(ValueError, match="altitude_unit"):
            sat.sync_position_from_satcom(altitude_unit="feet")

    def test_metre_is_the_default_unit(self):
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (0.0, 0.0, 550_000.0)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom()

        assert sat.position["altitude"] == pytest.approx(550.0)

    def test_skips_update_when_position_values_are_none(self):
        sat = Satellite("Sat0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (None, None, None)
        sat.satcom_object = MagicMock()
        sat.satcom_object.spatial_position = mock_pos

        sat.sync_position_from_satcom()

        assert sat.position == {}


class TestSatelliteEquality:
    """Satellite equality is inherited from Node."""

    def test_equal_same_name(self):
        s1 = Satellite("Sat1")
        s2 = Satellite("Sat1")
        assert s1 == s2

    def test_not_equal_different_name(self):
        s1 = Satellite("Sat1")
        s2 = Satellite("Sat2")
        assert s1 != s2

    def test_hash_matches_name_hash(self):
        sat = Satellite("Sat7")
        assert hash(sat) == hash("Sat7")


class TestSatelliteNetLogger:
    """The log destination is interpolated into a container shell command."""

    def test_log_path_is_quoted(self):
        sat = Satellite("Sat0")
        sat.interfaces.append(Interface(name="Sat0.1"))
        sat.execute_command = MagicMock()
        hostile_path = "/tmp/logs; touch /tmp/pwned"

        sat.start_net_logger(hostile_path)

        command = sat.execute_command.call_args.args[0]
        tokens = shlex.split(command)
        destination = hostile_path + "/Sat0_eth1.log"
        assert tokens[tokens.index("--log") + 1] == destination
        assert f"--log {destination}" not in command

    def test_normal_path_is_unchanged(self):
        sat = Satellite("Sat0")
        sat.interfaces.append(Interface(name="Sat0.1"))
        sat.execute_command = MagicMock()

        sat.start_net_logger("/tmp/logs")

        command = sat.execute_command.call_args.args[0]
        assert "--log /tmp/logs/Sat0_eth1.log" in command
