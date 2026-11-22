"""Regression tests for stable ground-to-space links.

Both custom strategies document that an existing connection is kept while the
satellite remains visible.  The legacy implementation marked every existing
GSL obsolete and appended a fresh connection for the retained satellite, which
tore the link down and recreated it on every connectivity tick.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sat_com_connectivity.ground_object_to_space_connectivity.models import (
    ComputedGroundObjectConnectionsResult,
)
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
    _retain_or_obsolete_existing_ground_links,
)

STRATEGIES = [LongestConnectionTimeStrategy, WeightedConnection]


def _make_ground_object(maximum_connected_satellites: int = 2) -> GroundObject:
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


def _satellite(index: int) -> Satellite:
    return Satellite(satellite_name=f"Sat{index}", topology_uniq_id=index)


def _visible(*pairs: tuple[Satellite, float]) -> list[VisibleSatellite]:
    return [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=elevation)
        for satellite, elevation in pairs
    ]


def _compute(strategy_cls, ground_object, satellites):
    with (
        patch(
            "satgonetem.utils.custom_connection_strategies.get_visible_satellites",
            return_value=satellites,
        ),
        # The estimator needs a propagated orbit; it is irrelevant here.
        patch.object(
            LongestConnectionTimeStrategy,
            "estimate_time_until_disconnection_2",
            return_value=100.0,
        ),
    ):
        return strategy_cls().compute_ground_object_connection_strategy(
            ground_object=ground_object,
            satellites=[visible.satellite for visible in satellites],
        )


EXPECTED_CHURN = "stable visible link must not be marked obsolete or created again"


def test_stable_visible_link_is_not_torn_down() -> None:
    for strategy_cls in STRATEGIES:
        ground_object = _make_ground_object(maximum_connected_satellites=1)
        satellite = _satellite(0)
        existing_link = GroundToSpaceLink(source=satellite, destination=ground_object)
        ground_object.links.append(existing_link)
        visible = _visible((satellite, 55.0))

        result = _compute(strategy_cls, ground_object, visible)

        assert result.obsolete_links == [], f"{strategy_cls.__name__}: {EXPECTED_CHURN}"
        assert result.new_ground_object_connections == [], (
            f"{strategy_cls.__name__}: {EXPECTED_CHURN}"
        )
        assert existing_link in ground_object.links


def test_retained_link_survives_repeated_connectivity_ticks() -> None:
    for strategy_cls in STRATEGIES:
        ground_object = _make_ground_object(maximum_connected_satellites=1)
        satellite = _satellite(0)
        existing_link = GroundToSpaceLink(source=satellite, destination=ground_object)
        ground_object.links.append(existing_link)
        visible = _visible((satellite, 55.0))

        first = _compute(strategy_cls, ground_object, visible)
        second = _compute(strategy_cls, ground_object, visible)

        for result in (first, second):
            assert result.obsolete_links == []
            assert result.new_ground_object_connections == []
        # Same link object throughout: no delete/re-add churn.
        assert ground_object.links == [existing_link]


def test_non_visible_existing_link_is_still_marked_obsolete() -> None:
    for strategy_cls in STRATEGIES:
        ground_object = _make_ground_object(maximum_connected_satellites=2)
        satellite = _satellite(0)
        existing_link = GroundToSpaceLink(source=satellite, destination=ground_object)
        ground_object.links.append(existing_link)

        result = _compute(strategy_cls, ground_object, [])

        assert result.obsolete_links == [existing_link]
        assert result.new_ground_object_connections == []


def test_only_links_beyond_capacity_are_marked_obsolete() -> None:
    for strategy_cls in STRATEGIES:
        ground_object = _make_ground_object(maximum_connected_satellites=2)
        satellites = [_satellite(index) for index in range(3)]
        links = [
            GroundToSpaceLink(source=satellite, destination=ground_object)
            for satellite in satellites
        ]
        ground_object.links.extend(links)
        visible = _visible(
            (satellites[0], 70.0),
            (satellites[1], 60.0),
            (satellites[2], 50.0),
        )

        result = _compute(strategy_cls, ground_object, visible)

        # The two highest-elevation links are retained; the third is removed.
        assert links[2] in result.obsolete_links
        assert links[0] not in result.obsolete_links
        assert links[1] not in result.obsolete_links
        assert result.new_ground_object_connections == []
        assert result.updated_links == []


def test_retained_link_leaves_room_for_one_new_connection() -> None:
    ground_object = _make_ground_object(maximum_connected_satellites=2)
    satellites = [_satellite(index) for index in range(2)]
    existing_link = GroundToSpaceLink(source=satellites[0], destination=ground_object)
    ground_object.links.append(existing_link)
    visible = _visible((satellites[0], 70.0), (satellites[1], 60.0))

    result = _compute(LongestConnectionTimeStrategy, ground_object, visible)

    assert existing_link not in result.obsolete_links
    assert [
        connection.satellite for connection in result.new_ground_object_connections
    ] == [satellites[1]]


def test_retains_the_highest_elevation_links_regardless_of_creation_order() -> None:
    """Budget overflow keeps the best links, not the oldest ones."""
    for strategy_cls in STRATEGIES:
        ground_object = _make_ground_object(maximum_connected_satellites=2)
        satellites = [_satellite(index) for index in range(3)]
        links = [
            GroundToSpaceLink(source=satellite, destination=ground_object)
            for satellite in satellites
        ]
        # Created lowest-elevation first, so creation order is the opposite
        # of the desired retention order.
        ground_object.links.extend(links)
        visible = _visible(
            (satellites[0], 50.0),
            (satellites[1], 60.0),
            (satellites[2], 70.0),
        )

        result = _compute(strategy_cls, ground_object, visible)

        assert result.obsolete_links == [links[0]], strategy_cls.__name__
        assert links[1] not in result.obsolete_links
        assert links[2] not in result.obsolete_links


def test_retention_falls_back_to_creation_order_without_elevation() -> None:
    """When elevation is unavailable, keep the previous deterministic order."""
    ground_object = _make_ground_object(maximum_connected_satellites=2)
    satellites = [_satellite(index) for index in range(3)]
    links = [
        GroundToSpaceLink(source=satellite, destination=ground_object)
        for satellite in satellites
    ]
    ground_object.links.extend(links)
    visible = [
        MagicMock(satellite=satellite, elevation_above_horizon=None)
        for satellite in satellites
    ]

    result = ComputedGroundObjectConnectionsResult()
    retained = _retain_or_obsolete_existing_ground_links(
        ground_object=ground_object,
        visible_satellites=visible,
        maximum_number_of_links=2,
        result=result,
    )

    assert retained == {id(satellites[0]), id(satellites[1])}
    assert result.obsolete_links == [links[2]]
