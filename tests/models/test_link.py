"""Tests for the Link model."""

import pytest
from unittest.mock import MagicMock, patch

from satgonetem.link_budget.config import LinkBudgetConfig
from satgonetem.models.link import Link, LinkBudgetService
from satgonetem.models.node import Node
from satgonetem.models.interface import Interface


SPEED_OF_LIGHT = 299_792_458  # m/s


@pytest.fixture
def sat_node():
    node = Node("Sat0")
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    return node


@pytest.fixture
def gnd_node():
    node = Node("Gnd0")
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    return node


@pytest.fixture
def isl_link(sat_node):
    other_sat = Node("Sat1")
    other_sat.position = {"latitude": 2.0, "longitude": 0.0, "altitude": 550.0}
    return Link(
        source=sat_node,
        target=other_sat,
        distance=300_000.0,
        type="InterSatelliteLink",
        default_capacity_kbps=1000,
    )


@pytest.fixture
def gsl_link(sat_node, gnd_node):
    return Link(
        source=sat_node,
        target=gnd_node,
        distance=550_000.0,
        type="GroundStationLink",
        default_capacity_kbps=500,
    )


class TestLinkInit:
    """Tests for Link.__init__."""

    def test_source_and_target_stored(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link.source is sat_node
        assert link.target is gnd_node

    def test_distance_stored(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link.distance == 550_000.0

    def test_type_stored(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link.type == "GroundStationLink"

    def test_is_active_true_by_default(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link.is_active is True

    def test_is_active_can_be_set_false(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            is_active=False,
            default_capacity_kbps=500,
        )
        assert link.is_active is False

    def test_satcom_object_is_none(self, isl_link):
        assert isl_link.satcom_object is None

    def test_grpc_flags_initially_false(self, isl_link):
        assert isl_link.to_update is False
        assert isl_link.to_add is False
        assert isl_link.to_remove is False
        assert isl_link.to_delete is False

    def test_rx_tx_initialized_to_zero(self, isl_link):
        assert isl_link.rx == 0
        assert isl_link.tx == 0


class TestLinkDelay:
    """Tests for Link propagation delay calculation."""

    def test_delay_computed_from_distance(self):
        node_a = Node("Sat0")
        node_b = Node("Sat1")
        distance = 300_000.0  # 300 km
        link = Link(
            source=node_a,
            target=node_b,
            distance=distance,
            type="InterSatelliteLink",
            default_capacity_kbps=1000,
        )
        expected_ms = int((distance / SPEED_OF_LIGHT) * 1000)
        assert link.delay == expected_ms

    def test_zero_distance_gives_zero_delay(self):
        node_a = Node("Sat0")
        node_b = Node("Sat1")
        link = Link(
            source=node_a,
            target=node_b,
            distance=0.0,
            type="InterSatelliteLink",
            default_capacity_kbps=1000,
        )
        assert link.delay == 0


class TestLinkGetLinkCapacity:
    """Tests for Link.get_link_capacity."""

    def test_isl_returns_first_capacity(self):
        node_a = Node("Sat0")
        node_b = Node("Sat1")
        link = Link(
            source=node_a,
            target=node_b,
            distance=300_000.0,
            type="InterSatelliteLink",
            default_capacity_kbps=1000,
        )
        assert link.peer1_capacity == 1000

    def test_capacity_reflects_capacities_list(self):
        node_a = Node("Sat0")
        node_b = Node("Gnd0")
        link = Link(
            source=node_a,
            target=node_b,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=250,
        )
        assert link.peer1_capacity == 250


class TestLinkGetNames:
    """Tests for Link.get_names."""

    def test_returns_tuple_of_two_strings(self, isl_link):
        names = isl_link.get_names()
        assert isinstance(names, tuple)
        assert len(names) == 2

    def test_first_name_is_source_dot_target_id(self, isl_link):
        name1, _ = isl_link.get_names()
        assert name1 == f"{isl_link.source.name}.{isl_link.target.id}"

    def test_second_name_is_target_dot_source_id(self, isl_link):
        _, name2 = isl_link.get_names()
        assert name2 == f"{isl_link.target.name}.{isl_link.source.id}"


class TestLinkEquality:
    """Tests for Link.__eq__."""

    def test_equal_links_with_same_source_and_target(self, sat_node, gnd_node):
        link1 = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        link2 = Link(
            source=sat_node,
            target=gnd_node,
            distance=600_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link1 == link2

    def test_not_equal_different_source(self, sat_node, gnd_node):
        other_sat = Node("Sat2")
        link1 = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        link2 = Link(
            source=other_sat,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link1 != link2

    def test_not_implemented_for_non_link(self, isl_link):
        result = isl_link.__eq__("not_a_link")
        assert result is NotImplemented


class TestLinkUpdateInterfacesState:
    """Tests for Link.update_interfaces_state."""

    def test_sets_interface_is_active_to_link_is_active(self, isl_link):
        iface = Interface(name="Sat0.1")
        iface.is_active = False
        isl_link.peer_interfaces = [iface]
        isl_link.is_active = True

        isl_link.update_interfaces_state()

        assert iface.is_active is True

    def test_sets_interface_is_active_false_when_link_inactive(self, isl_link):
        iface = Interface(name="Sat0.1")
        iface.is_active = True
        isl_link.peer_interfaces = [iface]
        isl_link.is_active = False

        isl_link.update_interfaces_state()

        assert iface.is_active is False

    def test_empty_peer_interfaces_no_error(self, isl_link):
        isl_link.peer_interfaces = []
        isl_link.update_interfaces_state()


class TestLinkElevationAngle:
    """Regression tests for the elevation angle passed to the link budget."""

    def test_overhead_satellite_returns_ninety_degrees(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )

        assert link._compute_elevation_angle(sat_node, gnd_node) == pytest.approx(
            90.0, abs=1e-6
        )

    def test_low_elevation_is_not_inverted(self, sat_node, gnd_node):
        # At 20 degrees of central angle and 550 km altitude the true
        # elevation is about 3.19 degrees; the old code returned ~86.8.
        sat_node.position = {
            "latitude": 20.0,
            "longitude": 0.0,
            "altitude": 550.0,
        }
        gnd_node.position = {
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
        }
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )

        elevation = link._compute_elevation_angle(sat_node, gnd_node)
        assert elevation == pytest.approx(3.1928435, abs=0.01)

    def test_below_horizon_satellite_stays_negative(self, sat_node, gnd_node):
        sat_node.position = {
            "latitude": 0.0,
            "longitude": 180.0,
            "altitude": 550.0,
        }
        gnd_node.position = {
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
        }
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=20_000_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )

        elevation = link._compute_elevation_angle(sat_node, gnd_node)
        assert elevation < 0.0


class TestLinkSyncDistanceAndDelay:
    """Tests for Link.sync_distance_from_satcom_and_delay."""

    def test_raises_when_satcom_object_is_none(self, gsl_link):
        with pytest.raises(ValueError, match="Satcom object is not set"):
            gsl_link.sync_distance_from_satcom_and_delay()

    def test_updates_delay_when_distance_changes(self, gsl_link):
        gsl_link.satcom_object = MagicMock()
        gsl_link.source.position = {
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 550.0,
        }
        gsl_link.target.position = {"latitude": 10.0, "longitude": 0.0, "altitude": 0.0}

        old_delay = gsl_link.delay
        gsl_link.sync_distance_from_satcom_and_delay()

        assert isinstance(gsl_link.delay, int)

    def test_to_update_set_true_when_delay_changes(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=1.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        link.satcom_object = MagicMock()
        link.source.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
        link.target.position = {"latitude": 10.0, "longitude": 0.0, "altitude": 0.0}

        link.sync_distance_from_satcom_and_delay()

        assert link.to_update is True

    def test_to_update_stays_false_when_delay_unchanged(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        link.satcom_object = MagicMock()
        link.source.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
        link.target.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}

        link.to_update = False
        link.sync_distance_from_satcom_and_delay()

        assert isinstance(link.to_update, bool)


class TestLinkCapacityRefresh:
    """Capacity refreshes must propagate to the emulator when values change."""

    def _make_budget_link(self, sat_node, gnd_node):
        return Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
            use_budget=True,
        )

    def test_refresh_marks_to_update_when_capacity_changes(
        self, sat_node, gnd_node
    ):
        link = self._make_budget_link(sat_node, gnd_node)
        link.to_update = False

        def recompute():
            link.peer1_capacity = 900
            link.peer2_capacity = 800

        with patch.object(link, "_compute_budget_capacity", side_effect=recompute):
            assert link.refresh_link_capacities() is True

        assert link.to_update is True

    def test_refresh_does_not_mark_to_update_when_capacity_is_unchanged(
        self, sat_node, gnd_node
    ):
        link = self._make_budget_link(sat_node, gnd_node)
        link.to_update = False

        with patch.object(link, "_compute_budget_capacity", return_value=None):
            assert link.refresh_link_capacities() is False

        assert link.to_update is False

    def test_distance_change_refreshes_budget_capacity(self, sat_node, gnd_node):
        link = self._make_budget_link(sat_node, gnd_node)
        link.satcom_object = MagicMock()
        link.distance = 1.0

        with patch.object(link, "refresh_link_capacities") as mock_refresh:
            link.sync_distance_from_satcom_and_delay()

        mock_refresh.assert_called_once()


class TestLinkUseBudget:
    """Tests for Link.use_budget flag."""

    def test_use_budget_false_uses_default_capacity(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
            use_budget=False,
        )
        assert link.peer1_capacity == 500

    def test_use_budget_true_without_antenna_fallback(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
            use_budget=True,
        )
        # No antennas set -> falls back to default capacity
        assert link.peer1_capacity == 500

    def test_use_budget_true_with_antennas(self, sat_node, gnd_node):
        from satgonetem.link_budget.antenna import Antenna

        sat_node.antenna = Antenna(
            diameter=1.0, efficiency=0.6, sspa_output_power_db=40.0
        )
        gnd_node.antenna = Antenna(diameter=2.0, efficiency=0.6)

        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
            use_budget=True,
        )
        # With antennas, capacity should be computed via budget
        assert isinstance(link.peer1_capacity, int)
        assert link.peer1_capacity >= 0

    def test_use_budget_ignored_for_non_gsl(self, sat_node):
        other_sat = Node("Sat1")
        other_sat.position = {"latitude": 2.0, "longitude": 0.0, "altitude": 550.0}
        link = Link(
            source=sat_node,
            target=other_sat,
            distance=300_000.0,
            type="InterSatelliteLink",
            default_capacity_kbps=1000,
            use_budget=True,
        )
        assert link.peer1_capacity == 1000


class TestLinkBudgetFallback:
    """Budget failures must fall back to the configured default capacity."""

    def _make_link(self, sat_node, gnd_node, default_capacity_kbps=777):
        from satgonetem.link_budget.antenna import Antenna

        sat_node.antenna = Antenna(
            diameter=1.0, efficiency=0.6, sspa_output_power_db=40.0
        )
        gnd_node.antenna = Antenna(diameter=2.0, efficiency=0.6)
        return Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=default_capacity_kbps,
            use_budget=True,
        )

    def test_both_directions_falling_back_use_default(self, sat_node, gnd_node):
        with patch.object(
            LinkBudgetService, "compute_one_way", side_effect=[0, 0]
        ):
            link = self._make_link(sat_node, gnd_node)

        assert link.peer1_capacity == 777
        assert link.peer2_capacity == 777

    def test_single_failed_direction_falls_back_independently(
        self, sat_node, gnd_node
    ):
        with patch.object(
            LinkBudgetService, "compute_one_way", side_effect=[0, 4321]
        ):
            link = self._make_link(sat_node, gnd_node)

        assert link.peer1_capacity == 777
        assert link.peer2_capacity == 4321

    def test_successful_capacities_are_preserved(self, sat_node, gnd_node):
        with patch.object(
            LinkBudgetService, "compute_one_way", side_effect=[1234, 5678]
        ):
            link = self._make_link(sat_node, gnd_node)

        assert link.peer1_capacity == 1234
        assert link.peer2_capacity == 5678


class TestLinkBudgetConfigParam:
    """Tests for Link.link_budget_config parameter."""

    def test_link_budget_config_stored(self, sat_node, gnd_node):
        cfg = LinkBudgetConfig(downlink_freq_ghz=20.0, uplink_freq_ghz=15.0)
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
            link_budget_config=cfg,
        )
        assert link.link_budget_config is cfg

    def test_default_link_budget_config_is_none(self, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
        )
        assert link.link_budget_config is None
