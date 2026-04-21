"""Tests for the GroundStation model."""

import pytest
from unittest.mock import MagicMock

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.node import Node


class TestGroundStationInit:
    """Tests for GroundStation.__init__."""

    def test_name_and_id(self):
        gs = GroundStation("Gnd0")
        assert gs.name == "Gnd0"
        assert gs.id == 0

    def test_multi_digit_id(self):
        gs = GroundStation("Gnd15")
        assert gs.id == 15

    def test_city_is_none_by_default(self):
        gs = GroundStation("Gnd0")
        assert gs.city is None

    def test_traffic_models_empty_by_default(self):
        gs = GroundStation("Gnd0")
        assert gs.traffic_models == []

    def test_inherits_from_node(self):
        gs = GroundStation("Gnd0")
        assert isinstance(gs, Node)

    def test_interfaces_empty(self):
        gs = GroundStation("Gnd0")
        assert gs.interfaces == []


class TestGroundStationSyncPositionFromSatcom:
    """Tests for GroundStation.sync_position_from_satcom."""

    def test_raises_when_satcom_object_not_set(self):
        gs = GroundStation("Gnd0")
        with pytest.raises((ValueError, AttributeError)):
            gs.sync_position_from_satcom()

    def test_updates_latitude_longitude_altitude(self):
        gs = GroundStation("Gnd0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (48.8, 2.3, 100.0)
        gs.satcom_object = MagicMock()
        gs.satcom_object.spatial_position = mock_pos

        gs.sync_position_from_satcom()

        assert gs.position["latitude"] == pytest.approx(48.8)
        assert gs.position["longitude"] == pytest.approx(2.3)
        assert gs.position["altitude"] == pytest.approx(0.1)

    def test_skips_update_when_position_values_are_none(self):
        gs = GroundStation("Gnd0")
        mock_pos = MagicMock()
        mock_pos.to_latitude_longitude_altitude.return_value = (None, None, None)
        gs.satcom_object = MagicMock()
        gs.satcom_object.spatial_position = mock_pos

        gs.sync_position_from_satcom()

        assert gs.position == {}


class TestGroundStationAddTraffic:
    """Tests for GroundStation.add_traffic."""

    def test_add_single_traffic_model(self):
        gs = GroundStation("Gnd0")
        model = object()
        gs.add_traffic(model)
        assert gs.traffic_models == [model]

    def test_add_list_of_traffic_models(self):
        gs = GroundStation("Gnd0")
        models = [object(), object()]
        gs.add_traffic(models)
        assert gs.traffic_models == models

    def test_add_traffic_multiple_times(self):
        gs = GroundStation("Gnd0")
        m1 = object()
        m2 = object()
        gs.add_traffic(m1)
        gs.add_traffic(m2)
        assert len(gs.traffic_models) == 2
        assert m1 in gs.traffic_models
        assert m2 in gs.traffic_models

    def test_add_empty_list_does_not_change_models(self):
        gs = GroundStation("Gnd0")
        gs.add_traffic([])
        assert gs.traffic_models == []


class TestGroundStationEquality:
    """GroundStation equality is inherited from Node."""

    def test_equal_same_name(self):
        g1 = GroundStation("Gnd1")
        g2 = GroundStation("Gnd1")
        assert g1 == g2

    def test_not_equal_different_name(self):
        g1 = GroundStation("Gnd1")
        g2 = GroundStation("Gnd2")
        assert g1 != g2

    def test_hash_matches_name_hash(self):
        gs = GroundStation("Gnd3")
        assert hash(gs) == hash("Gnd3")
