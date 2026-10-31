"""Regression tests for UTIL-03.

``LongestConnectionTimeStrategy`` used to store ``None`` disconnection
estimates and then sort them, raising ``TypeError``.  The estimator also used
to return ``None`` for "no future crossing", even though its annotated return
type is ``float``.
"""

import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from sgp4.api import jday

from sat_com_connectivity.ground_object_to_space_connectivity.models import (
    ComputedGroundObjectConnectionsResult,
)
from sat_com_connectivity.ground_object_to_space_connectivity.visibility_angle_utils import (
    VisibleSatellite,
)
from sat_com_model.models import (
    GroundObject,
    GroundObjectDomain,
    Satellite,
    SpatialPosition,
)

from satgonetem.utils.custom_connection_strategies import (
    LongestConnectionTimeStrategy,
)

WGS84_SEMI_MAJOR_AXIS_M = 6378137.0
EARTH_ROTATION_RATE_RAD_S = 7.2921158553e-5
ESTIMATOR_MAX_TIME_S = 3600.0


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

    # The estimator computes: v_ecef = R @ v_teme + [omega*y, -omega*x, 0]
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
    movement_model.get_position_earth_general_inertial.return_value = (
        r_teme_km,
        v_teme_km_s,
    )

    satellite = MagicMock()
    satellite.satellite_name = "TestSat"
    satellite.movement_model = movement_model
    return satellite


def test_strategy_sorts_none_estimate_as_max_time_without_type_error():
    ground_object = _make_ground_object(maximum_connected_satellites=2)
    satellites = [Satellite(satellite_name=f"Sat{index}") for index in range(3)]
    visible_satellites = [
        VisibleSatellite(satellite=satellite, elevation_above_horizon=elevation)
        for satellite, elevation in zip(satellites, [60.0, 45.0, 30.0])
    ]
    estimates = {"Sat0": None, "Sat1": 120.0, "Sat2": 30.0}

    def fake_estimator(**kwargs):
        return estimates[kwargs["satellite"].satellite_name]

    strategy = LongestConnectionTimeStrategy()
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
        result = strategy.compute_ground_object_connection_strategy(
            ground_object=ground_object,
            satellites=satellites,
        )

    assert isinstance(result, ComputedGroundObjectConnectionsResult)
    # None is coerced to max_time (3600 s), so it must be ranked first; the
    # 30 s satellite is then dropped because only two links are allowed.
    assert [
        connection.satellite.satellite_name
        for connection in result.new_ground_object_connections
    ] == ["Sat0", "Sat1"]


def test_estimator_returns_finite_float_when_no_future_crossing_exists():
    """Radially receding satellite: both quadratic roots are negative."""
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    ground_object = _make_ground_object()
    position_ecef_km = np.array(
        [WGS84_SEMI_MAJOR_AXIS_M / 1000.0 + 700.0, 0.0, 0.0]
    )
    velocity_ecef_km_s = np.array([1.0, 0.0, 0.0])  # straight up, away from Earth
    satellite = _make_satellite_with_ecef_state(
        when, position_ecef_km, velocity_ecef_km_s
    )

    estimate = LongestConnectionTimeStrategy().estimate_time_until_disconnection_2(
        ground_object=ground_object,
        satellite=satellite,
        minimum_elevation=10.0,
        max_time=ESTIMATOR_MAX_TIME_S,
    )

    assert isinstance(estimate, float)
    assert np.isfinite(estimate)
    assert estimate == pytest.approx(ESTIMATOR_MAX_TIME_S)


def test_estimator_returns_zero_when_already_at_minimum_elevation():
    """A satellite on the horizon is not connected for any positive time."""
    when = datetime.datetime(2024, 1, 1, 12, 0, 0)
    ground_object = _make_ground_object()
    position_ecef_km = np.array(
        [WGS84_SEMI_MAJOR_AXIS_M / 1000.0, 700.0, 0.0]  # elevation ~ 0 degrees
    )
    velocity_ecef_km_s = np.array([0.0, 0.0, 1.0])
    satellite = _make_satellite_with_ecef_state(
        when, position_ecef_km, velocity_ecef_km_s
    )

    estimate = LongestConnectionTimeStrategy().estimate_time_until_disconnection_2(
        ground_object=ground_object,
        satellite=satellite,
        minimum_elevation=10.0,
        max_time=ESTIMATOR_MAX_TIME_S,
    )

    assert isinstance(estimate, float)
    assert np.isfinite(estimate)
    assert estimate == 0.0
