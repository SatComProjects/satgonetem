"""Regression tests for UTIL-02.

Both custom strategies used to keep existing connections in a first pass and
then iterate over *all* visible satellites in a second pass.  Retained
satellites were therefore appended twice, producing duplicate
``GroundToSpaceConnection`` objects for the same satellite.
"""

from unittest.mock import patch

from sat_com_connectivity.ground_object_to_space_connectivity.visibility_angle_utils import (
    VisibleSatellite,
)
from sat_com_model.models import (
    GroundObject,
    GroundObjectDomain,
    GroundToSpaceLink,
    Satellite,
    SpatialPosition,
)

from satgonetem.utils.custom_connection_strategies import (
    LongestConnectionTimeStrategy,
    WeightedConnection,
)


def _make_ground_object(maximum_connected_satellites: int = 3) -> GroundObject:
    return GroundObject(
        spatial_position_info=SpatialPosition(
            latitude=0.0,
            longitude=0.0,
            altitude=0.0,
        ),
        ground_object_domain=GroundObjectDomain(
            identifier="ground-station-domain",
            type="ground_station",
            elevation_above_horizon=10.0,
            maximum_connected_satellites=maximum_connected_satellites,
        ),
    )


def _make_visible_satellites():
    """Four visible satellites; ``Sat0`` already has a link.

    More satellites are visible than the maximum number of connections so the
    second pass really runs and would previously re-append the retained one.
    """
    ground_object = _make_ground_object()
    # ``TopologyObject.__eq__`` compares ``topology_uniq_id``; give each
    # satellite a distinct id so the retained one is unambiguous.
    satellites = [
        Satellite(satellite_name=f"Sat{index}", topology_uniq_id=index)
        for index in range(4)
    ]
    ground_object.links.append(
        GroundToSpaceLink(source=satellites[0], destination=ground_object)
    )
    visible_satellites = [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=elevation)
        for satellite, elevation in zip(satellites, [70.0, 60.0, 50.0, 40.0])
    ]
    return ground_object, satellites, visible_satellites


def _assert_each_connected_satellite_appears_once(result, retained_satellite_name):
    connected_names = [
        connection.satellite.satellite_name
        for connection in result.new_ground_object_connections
    ]
    assert len(connected_names) == 3, connected_names
    assert len(connected_names) == len(set(connected_names)), (
        f"duplicate connections: {connected_names}"
    )
    assert retained_satellite_name in connected_names


def test_longest_connection_time_strategy_does_not_duplicate_retained_satellite():
    ground_object, satellites, visible_satellites = _make_visible_satellites()
    estimates = {"Sat0": 300.0, "Sat1": 200.0, "Sat2": 100.0, "Sat3": 50.0}

    def fake_estimator(**kwargs):
        return estimates[kwargs["satellite"].satellite_name]

    with (
        patch(
            "satgonetem.utils.custom_connection_strategies.get_visible_satellites",
            return_value=visible_satellites,
        ),
        patch.object(
            LongestConnectionTimeStrategy,
            "estimate_time_until_disconnection_2",
            side_effect=fake_estimator,
        ),
        patch("builtins.print"),
    ):
        result = LongestConnectionTimeStrategy().compute_ground_object_connection_strategy(
            ground_object=ground_object,
            satellites=satellites,
        )

    _assert_each_connected_satellite_appears_once(result, "Sat0")


def test_weighted_connection_does_not_duplicate_retained_satellite():
    ground_object, satellites, visible_satellites = _make_visible_satellites()

    with patch(
        "satgonetem.utils.custom_connection_strategies.get_visible_satellites",
        return_value=visible_satellites,
    ):
        result = WeightedConnection().compute_ground_object_connection_strategy(
            ground_object=ground_object,
            satellites=satellites,
        )

    _assert_each_connected_satellite_appears_once(result, "Sat0")
