"""Behavioural tests for the remaining branches of the custom strategies.

Covers the analytic estimator's degenerate/edge cases and the closed-form
bisection estimator, plus the early return that keeps existing links when they
already fill the ground object's connection budget.

The satellite stubs inject an ECEF position and velocity directly into the
movement model boundary; nothing here propagates a real orbit or touches the
network.
"""

import datetime
import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from sat_com_connectivity.ground_object_to_space_connectivity.visibility_angle_utils import (
    VisibleSatellite,
)
from sat_com_connectivity.utils import get_elevation_angle
from sat_com_model.models import (
    GroundObject,
    GroundObjectDomain,
    GroundToSpaceLink,
    Satellite,
    SpatialPosition,
)
from sgp4.api import jday

from satgonetem.utils.custom_connection_strategies import (
    LongestConnectionTimeStrategy,
    WeightedConnection,
)

WGS84_SEMI_MAJOR_AXIS_M = 6378137.0
EARTH_ROTATION_RATE_RAD_S = 7.2921158553e-5
ESTIMATOR_MAX_TIME_S = 3600.0
SATELLITE_ALTITUDE_M = 700_000.0


def _make_ground_object(
    *,
    latitude: float = 0.0,
    longitude: float = 0.0,
    altitude: float = 0.0,
    maximum_connected_satellites: int = 2,
) -> GroundObject:
    return GroundObject(
        spatial_position_info=SpatialPosition(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
        ),
        ground_object_domain=GroundObjectDomain(
            identifier="ground-station-domain",
            type="ground_station",
            elevation_above_horizon=10.0,
            maximum_connected_satellites=maximum_connected_satellites,
        ),
    )


def _gmst_rad(t_utc: datetime.datetime) -> float:
    """GMST for the exact same IAU-1982 model used by the estimator."""
    jd, fr = jday(
        t_utc.year,
        t_utc.month,
        t_utc.day,
        t_utc.hour,
        t_utc.minute,
        t_utc.second + t_utc.microsecond * 1e-6,
    )
    jd_utc = jd + fr
    t = (jd_utc - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd_utc - 2451545.0)
        + 0.000387933 * t**2
        - (t**3) / 38710000.0
    )
    return np.deg2rad(gmst_deg % 360.0)


def _ecef_to_teme(vector_ecef_km: np.ndarray, theta: float) -> np.ndarray:
    """Inverse of the estimator's TEME -> ECEF Z-rotation."""
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    return np.array(
        [
            cos_t * vector_ecef_km[0] - sin_t * vector_ecef_km[1],
            sin_t * vector_ecef_km[0] + cos_t * vector_ecef_km[1],
            vector_ecef_km[2],
        ]
    )


def _make_satellite_with_ecef_state(
    when: datetime.datetime,
    position_ecef_km: np.ndarray,
    velocity_ecef_km_s: np.ndarray,
):
    """Build a satellite stub whose movement model returns a chosen ECEF state.

    The estimator converts TEME -> ECEF and then applies the Earth-rotation
    velocity correction, so the TEME values are pre-rotated to make the
    post-conversion ECEF state exactly the requested one.
    """
    theta = _gmst_rad(when)

    pre_rotation_velocity = velocity_ecef_km_s - np.array(
        [
            EARTH_ROTATION_RATE_RAD_S * position_ecef_km[1],
            -EARTH_ROTATION_RATE_RAD_S * position_ecef_km[0],
            0.0,
        ]
    )
    r_teme_km = _ecef_to_teme(position_ecef_km, theta)
    v_teme_km_s = _ecef_to_teme(pre_rotation_velocity, theta)

    movement_model = MagicMock()
    movement_model.clock.get_current_time.return_value = when
    movement_model.get_position_earth_general_inertial.return_value = r_teme_km
    movement_model.get_velocity_earth_general_inertial.return_value = v_teme_km_s

    satellite = MagicMock()
    satellite.satellite_name = "TestSat"
    satellite.movement_model = movement_model
    return satellite


def _estimate(
    position_ecef_km: np.ndarray,
    velocity_ecef_km_s: np.ndarray,
    *,
    minimum_elevation: float = 10.0,
    max_time: float = ESTIMATOR_MAX_TIME_S,
    ground_object: GroundObject | None = None,
) -> float:
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    satellite = _make_satellite_with_ecef_state(
        when, position_ecef_km, velocity_ecef_km_s
    )
    return LongestConnectionTimeStrategy().estimate_time_until_disconnection_2(
        ground_object=ground_object or _make_ground_object(),
        satellite=satellite,
        minimum_elevation=minimum_elevation,
        max_time=max_time,
    )


def test_estimator_returns_zero_when_at_minimum_elevation():
    """A satellite exactly on the mask boundary has no positive exit time."""
    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0, 700.0, 0.0]),
        np.array([0.0, 0.0, 1.0]),
    )

    assert estimate == 0.0


def test_estimator_returns_max_time_when_all_roots_are_negative():
    """A gently receding satellite has no future mask crossing."""
    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 700.0, 0.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
    )

    assert estimate == ESTIMATOR_MAX_TIME_S


def test_estimator_returns_zero_for_invalid_ground_station_position():
    """A null ground-station ECEF vector cannot establish any connection."""
    ground_object = _make_ground_object(altitude=-WGS84_SEMI_MAJOR_AXIS_M)
    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 700.0, 0.0, 0.0]),
        np.array([0.0, 7.5, 0.0]),
        ground_object=ground_object,
    )

    assert estimate == 0.0


def test_estimator_degenerate_linear_branch_solves_known_crossing_time():
    """When A vanishes, the linear root -C/B is the exact exit time.

    The station sits on the equator, the satellite is 1 km above it and moves
    diagonally at 1 m/s with a 45 degree mask, which makes the quadratic term
    cancel to below the estimator's 1e-12 tolerance.
    """
    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 1.0, 0.0, 0.0]),
        np.array([-0.001, 0.001, 0.0]),
        minimum_elevation=45.0,
    )

    assert estimate == pytest.approx(500.0, rel=1e-9)
    assert estimate < ESTIMATOR_MAX_TIME_S


def test_estimator_degenerate_linear_branch_clamps_to_max_time():
    """The linear root is clamped when it exceeds the caller's horizon."""
    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 1.0, 0.0, 0.0]),
        np.array([-0.001, 0.001, 0.0]),
        minimum_elevation=45.0,
        max_time=100.0,
    )

    assert estimate == 100.0


def test_estimator_degenerate_linear_no_vertical_motion_returns_max_time():
    """A stationary satellite never drops below a 0 degree mask.

    Reusing the time-derived TEME state would leave a rounding-level velocity
    component in ``A``/``B``.  A zero TEME velocity makes the degenerate
    linear coefficients exactly zero, which is the branch under test.
    """
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    position_ecef_km = np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 1.0, 0.0, 0.0])
    satellite = _make_satellite_with_ecef_state(
        when, position_ecef_km, np.array([0.0, 0.0, 0.0])
    )
    satellite.movement_model.get_velocity_earth_general_inertial.return_value = (
        np.zeros(3)
    )

    estimate = LongestConnectionTimeStrategy().estimate_time_until_disconnection_2(
        ground_object=_make_ground_object(),
        satellite=satellite,
        minimum_elevation=0.0,
        max_time=ESTIMATOR_MAX_TIME_S,
    )

    assert estimate == ESTIMATOR_MAX_TIME_S


def test_estimator_returns_max_time_for_radially_receding_satellite():
    """Radial recession from zenith keeps the elevation at 90 degrees."""
    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 1900.0, 0.0, 0.0]),
        np.array([11.89, 0.0, 0.0]),
    )

    assert estimate == ESTIMATOR_MAX_TIME_S


def test_estimator_returns_analytic_exit_time_for_tangential_descent():
    """A satellite crossing the mask tangentially exits after h / (v tan eps)."""
    altitude_km = 500.0
    speed_km_s = 1.0
    expected = altitude_km / (speed_km_s * np.tan(np.radians(10.0)))

    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + altitude_km, 0.0, 0.0]),
        np.array([0.0, 0.0, -speed_km_s]),
    )

    assert estimate == pytest.approx(expected, rel=1e-12)
    assert estimate < ESTIMATOR_MAX_TIME_S


def test_estimator_clamps_tangential_exit_time_to_max_time():
    """The same geometry from 1000 km exits after ~5671 s, above the horizon."""
    unclamped = 1000.0 / np.tan(np.radians(10.0))
    assert unclamped > ESTIMATOR_MAX_TIME_S

    estimate = _estimate(
        np.array([WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 1000.0, 0.0, 0.0]),
        np.array([0.0, 0.0, -1.0]),
    )

    assert estimate == ESTIMATOR_MAX_TIME_S


def _latitude_for_elevation(
    elevation_deg: float, altitude_m: float = SATELLITE_ALTITUDE_M
) -> float:
    """Invert the WGS-72 elevation geometry for a satellite over the equator."""
    low, high = 0.0, 80.0
    for _ in range(80):
        mid = 0.5 * (low + high)
        elevation = get_elevation_angle((mid, 0.0, altitude_m), (0.0, 0.0, 0.0))
        if elevation > elevation_deg:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def _elevation_at(
    latitude_deg: float, altitude_m: float = SATELLITE_ALTITUDE_M
) -> float:
    return get_elevation_angle((latitude_deg, 0.0, altitude_m), (0.0, 0.0, 0.0))


def _make_bisection_satellite(when, get_lonlatalt, position_at=None):
    movement_model = MagicMock()
    movement_model.clock.get_current_time.return_value = when
    movement_model.orbital_object.get_lonlatalt.side_effect = get_lonlatalt
    if position_at is not None:
        movement_model.get_position_at.side_effect = position_at

    satellite = MagicMock()
    satellite.satellite_name = "BisectSat"
    satellite.movement_model = movement_model
    return satellite


def _run_bisection_estimator(
    satellite, *, minimum_elevation: float = 10.0, max_time: float = 300.0
) -> float:
    return LongestConnectionTimeStrategy().estimate_time_until_disconnection(
        ground_object=_make_ground_object(),
        satellite=satellite,
        minimum_elevation=minimum_elevation,
        max_time=max_time,
    )


def test_bisection_estimator_raises_when_satellite_is_below_elevation_mask():
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    satellite = _make_bisection_satellite(
        when,
        lambda _t: (0.0, 180.0, SATELLITE_ALTITUDE_M),
    )

    with pytest.raises(ValueError) as exc_info:
        _run_bisection_estimator(satellite)

    message = str(exc_info.value)
    assert "BisectSat" in message
    assert "already below minimum elevation" in message


def test_bisection_estimator_finds_known_crossing_time():
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    crossing_seconds = 500.0
    crossing_latitude = _latitude_for_elevation(10.0)
    latitude_rate_deg_per_s = crossing_latitude / crossing_seconds
    assert _elevation_at(crossing_latitude) == pytest.approx(10.0, abs=1e-6)

    satellite = _make_bisection_satellite(
        when,
        lambda t: (
            0.0,
            latitude_rate_deg_per_s * (t - when).total_seconds(),
            SATELLITE_ALTITUDE_M,
        ),
    )

    estimate = _run_bisection_estimator(satellite, max_time=ESTIMATOR_MAX_TIME_S)

    assert estimate == pytest.approx(crossing_seconds, abs=1e-3)
    assert estimate < ESTIMATOR_MAX_TIME_S
    # Just before the reported crossing the satellite is still visible.
    assert _elevation_at(latitude_rate_deg_per_s * (estimate - 1.0)) > 10.0
    assert _elevation_at(latitude_rate_deg_per_s * estimate) <= 10.0 + 1e-9


def test_bisection_estimator_returns_max_time_and_warns_when_never_below(caplog):
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    satellite = _make_bisection_satellite(
        when,
        lambda _t: (0.0, 0.0, SATELLITE_ALTITUDE_M),
    )

    with caplog.at_level(logging.WARNING):
        estimate = _run_bisection_estimator(satellite, max_time=300.0)

    assert estimate == 300.0
    assert any(
        "stays above" in record.getMessage() and "BisectSat" in record.getMessage()
        for record in caplog.records
    )


def _raise_missing_lonlatalt(_t):
    raise AttributeError("no lonlatalt")


def test_bisection_estimator_falls_back_to_get_position_at():
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    satellite = _make_bisection_satellite(
        when,
        _raise_missing_lonlatalt,
        position_at=lambda _t: (0.0, 0.0, SATELLITE_ALTITUDE_M),
    )

    estimate = _run_bisection_estimator(satellite, max_time=300.0)

    assert estimate == 300.0
    assert satellite.movement_model.orbital_object.get_lonlatalt.called
    assert satellite.movement_model.get_position_at.called


def _make_retained_capacity_scenario():
    ground_object = _make_ground_object(maximum_connected_satellites=1)
    satellite = Satellite(satellite_name="Retained", topology_uniq_id=1)
    existing_link = GroundToSpaceLink(source=satellite, destination=ground_object)
    ground_object.links.append(existing_link)
    visible_satellites = [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=55.0)
    ]
    return ground_object, satellite, existing_link, visible_satellites


def test_longest_connection_strategy_stops_when_retained_links_fill_capacity():
    ground_object, satellite, existing_link, visible_satellites = (
        _make_retained_capacity_scenario()
    )

    with (
        patch(
            "satgonetem.utils.custom_connection_strategies.get_visible_satellites",
            return_value=visible_satellites,
        ),
        patch.object(
            LongestConnectionTimeStrategy,
            "estimate_time_until_disconnection_2",
            side_effect=AssertionError("estimator must not run when capacity is full"),
        ),
    ):
        result = (
            LongestConnectionTimeStrategy().compute_ground_object_connection_strategy(
                ground_object=ground_object,
                satellites=[satellite],
            )
        )

    assert [
        connection.satellite for connection in result.new_ground_object_connections
    ] == [satellite]
    assert existing_link in result.obsolete_links


def test_weighted_connection_stops_when_retained_links_fill_capacity():
    ground_object, satellite, existing_link, visible_satellites = (
        _make_retained_capacity_scenario()
    )

    with patch(
        "satgonetem.utils.custom_connection_strategies.get_visible_satellites",
        return_value=visible_satellites,
    ):
        result = WeightedConnection().compute_ground_object_connection_strategy(
            ground_object=ground_object,
            satellites=[satellite],
        )

    assert [
        connection.satellite for connection in result.new_ground_object_connections
    ] == [satellite]
    assert existing_link in result.obsolete_links


def test_longest_connection_strategy_sorts_estimates_and_respects_capacity():
    ground_object = _make_ground_object(maximum_connected_satellites=2)
    satellites = [
        Satellite(satellite_name=f"Sat{index}", topology_uniq_id=index)
        for index in range(3)
    ]
    visible_satellites = [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=elevation)
        for satellite, elevation in zip(satellites, [60.0, 50.0, 40.0], strict=True)
    ]
    estimates = {"Sat0": 100.0, "Sat1": None, "Sat2": 50.0}

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
        result = (
            LongestConnectionTimeStrategy().compute_ground_object_connection_strategy(
                ground_object=ground_object,
                satellites=satellites,
            )
        )

    # None is treated as the largest possible connection time, so Sat1 wins;
    # Sat0 is second and Sat2 is dropped by the capacity limit.
    assert [
        connection.satellite.satellite_name
        for connection in result.new_ground_object_connections
    ] == ["Sat1", "Sat0"]


def test_longest_connection_strategy_skips_retained_satellite_in_second_pass():
    ground_object = _make_ground_object(maximum_connected_satellites=2)
    satellites = [
        Satellite(satellite_name=f"Sat{index}", topology_uniq_id=index)
        for index in range(3)
    ]
    existing_link = GroundToSpaceLink(source=satellites[0], destination=ground_object)
    ground_object.links.append(existing_link)
    visible_satellites = [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=elevation)
        for satellite, elevation in zip(satellites, [70.0, 60.0, 50.0], strict=True)
    ]
    estimates = {"Sat1": 300.0, "Sat2": 200.0}

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
        result = (
            LongestConnectionTimeStrategy().compute_ground_object_connection_strategy(
                ground_object=ground_object,
                satellites=satellites,
            )
        )

    assert [
        connection.satellite.satellite_name
        for connection in result.new_ground_object_connections
    ] == ["Sat0", "Sat1"]
    assert existing_link in result.obsolete_links


def test_weighted_connection_keeps_retained_link_then_fills_remaining_capacity():
    ground_object = _make_ground_object(maximum_connected_satellites=2)
    satellites = [
        Satellite(satellite_name=f"Sat{index}", topology_uniq_id=index)
        for index in range(3)
    ]
    existing_link = GroundToSpaceLink(source=satellites[0], destination=ground_object)
    ground_object.links.append(existing_link)
    visible_satellites = [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=elevation)
        for satellite, elevation in zip(satellites, [70.0, 60.0, 50.0], strict=True)
    ]

    with patch(
        "satgonetem.utils.custom_connection_strategies.get_visible_satellites",
        return_value=visible_satellites,
    ):
        result = WeightedConnection().compute_ground_object_connection_strategy(
            ground_object=ground_object,
            satellites=satellites,
        )

    assert [
        connection.satellite.satellite_name
        for connection in result.new_ground_object_connections
    ] == ["Sat0", "Sat1"]
    assert existing_link in result.obsolete_links
