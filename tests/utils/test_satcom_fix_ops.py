"""Behavioural tests for the sat_com monkey patch and its orbital model.

The tests use the real SGP4 propagator for successful propagation, a stubbed
orbital record for the error paths, and fake modules for the patch installer so
that no global sat_com_trajectopy state is mutated.
"""

import datetime
import math
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sat_com_model.models import SpatialPosition
from sgp4.api import jday

import satgonetem.utils.satcom_fix as satcom_fix
from satgonetem.utils.satcom_fix import (
    PyOrbitalModel,
    _3d_distance_km,
    _3d_distance_meters,
    _ground_distance_km,
    _ground_distance_meters,
    apply_satcom_fix,
    get_half_cone_angle,
)

TLE_LINE_1 = "1 44057U 19010A   26126.83484127  .00000046  00000+0  86073-4 0  9992"
TLE_LINE_2 = "2 44057  87.9096 236.9957 0002389 103.2080 256.9318 13.16595320346123"
TLE = {
    "satellite_name": "ONEWEB-0012",
    "line1": TLE_LINE_1,
    "line2": TLE_LINE_2,
}

# Two points one degree of longitude apart on the equator; the great-circle
# distance is 2 * 6371.009 km * asin(sin(0.5 degree)).
ONE_DEGREE_GROUND_DISTANCE_KM = 111.19508372419142


class _Clock:
    """Minimal TimeManager stand-in with a mutable current time."""

    def __init__(self, current):
        self.current = current

    def get_current_time(self):
        return self.current


def _model_with_sgp4(clock, sgp4_result):
    model = PyOrbitalModel(TLE, clock)
    model.orbital_object = MagicMock()
    model.orbital_object.sgp4.return_value = sgp4_result
    return model


def test_valid_tle_dict_initialises_model():
    clock = _Clock(datetime.datetime(2026, 5, 6, 12, 0, 0))

    model = PyOrbitalModel({**TLE, "satellite_name": "  ONEWEB-0012  "}, clock)

    assert model.satellite_name == "ONEWEB-0012"
    assert model.line1 == TLE_LINE_1
    assert model.line2 == TLE_LINE_2
    assert model.clock is clock
    assert callable(model.orbital_object.sgp4)


@pytest.mark.parametrize(
    "tle",
    [
        {"line1": TLE_LINE_1, "line2": TLE_LINE_2},
        {**TLE, "satellite_name": ""},
        {**TLE, "satellite_name": "   "},
        {**TLE, "satellite_name": 42},
    ],
    ids=["missing", "empty", "blank", "non-string"],
)
def test_invalid_satellite_name_raises_value_error(tle):
    with pytest.raises(ValueError, match="Invalid or missing satellite name"):
        PyOrbitalModel(tle, _Clock(datetime.datetime(2026, 5, 6)))


@pytest.mark.parametrize(
    "tle",
    [
        {"satellite_name": "ONEWEB-0012"},
        {"satellite_name": "ONEWEB-0012", "line1": TLE_LINE_1},
        {"satellite_name": "ONEWEB-0012", "line1": "", "line2": TLE_LINE_2},
        {"satellite_name": "ONEWEB-0012", "line1": TLE_LINE_1, "line2": "   "},
        {"satellite_name": "ONEWEB-0012", "line1": 123, "line2": TLE_LINE_2},
    ],
    ids=["missing-both", "missing-line2", "empty-line1", "blank-line2", "non-string"],
)
def test_invalid_tle_lines_raise_value_error(tle):
    with pytest.raises(ValueError, match="Invalid or missing TLE lines"):
        PyOrbitalModel(tle, _Clock(datetime.datetime(2026, 5, 6)))


@pytest.mark.parametrize(
    ("error_code", "message_fragment"),
    [
        (1, "Mean eccentricity is outside the range"),
        (6, "Orbit has decayed"),
        (5, "Unknown SGP4 error"),
    ],
)
def test_compute_position_raises_on_sgp4_error(error_code, message_fragment):
    model = _model_with_sgp4(
        _Clock(datetime.datetime(2026, 5, 6, 12, 0, 0)),
        (error_code, None, None),
    )

    with pytest.raises(RuntimeError) as exc_info:
        model._compute_position_at(datetime.datetime(2026, 5, 6, 12, 0, 0))

    message = str(exc_info.value)
    assert f"propagation error {error_code}" in message
    assert message_fragment in message


def test_get_longitude_latitude_raises_when_clock_returns_none():
    model = PyOrbitalModel(TLE, _Clock(None))

    with pytest.raises(RuntimeError, match="Clock returned None"):
        model.get_longitude_latitude()


def test_get_longitude_latitude_caches_identical_timestamp():
    when = datetime.datetime(2026, 5, 6, 12, 0, 0)
    clock = _Clock(when)
    model = PyOrbitalModel(TLE, clock)

    first = model.get_longitude_latitude()
    second = model.get_longitude_latitude()
    assert first is second

    clock.current = when + datetime.timedelta(seconds=1)
    third = model.get_longitude_latitude()
    assert third is not first
    assert third.longitude != first.longitude or third.latitude != first.latitude


def test_get_longitude_latitude_altitude_uses_target_epoch_for_delta():
    when = datetime.datetime(2026, 5, 6, 12, 0, 0)
    delta_seconds = 60.0
    model = PyOrbitalModel(TLE, _Clock(when))
    model_at_target = PyOrbitalModel(
        TLE, _Clock(when + datetime.timedelta(seconds=delta_seconds))
    )

    shifted = model.get_longitude_latitude_altitude(delta_seconds)
    direct = model_at_target.get_longitude_latitude()

    assert shifted.latitude == pytest.approx(direct.latitude, abs=1e-12)
    assert shifted.longitude == pytest.approx(direct.longitude, abs=1e-12)
    assert shifted.altitude == pytest.approx(direct.altitude, abs=1e-6)


def test_get_longitude_latitude_altitude_raises_when_clock_returns_none():
    model = PyOrbitalModel(TLE, _Clock(None))

    with pytest.raises(RuntimeError, match="Clock returned None"):
        model.get_longitude_latitude_altitude(delta_t=60.0)


def test_get_longitude_latitude_altitude_zero_delta_reuses_cached_position():
    model = PyOrbitalModel(TLE, _Clock(datetime.datetime(2026, 5, 6, 12, 0, 0)))

    cached = model.get_longitude_latitude()
    delegated = model.get_longitude_latitude_altitude(0.0)

    assert delegated is cached


def test_get_datetime_returns_clock_time():
    when = datetime.datetime(2026, 5, 6, 12, 0, 0)
    model = PyOrbitalModel(TLE, _Clock(when))

    assert model.get_datetime() is when


def test_compute_position_normalises_aware_time_to_utc():
    model = PyOrbitalModel(TLE, None)
    utc_when = datetime.datetime(2026, 5, 6, 12, 34, 56, tzinfo=datetime.timezone.utc)
    local_when = utc_when.astimezone(datetime.timezone(datetime.timedelta(hours=2)))

    utc_position = model._compute_position_at(utc_when)
    local_position = model._compute_position_at(local_when)

    assert local_position.latitude == pytest.approx(utc_position.latitude, abs=1e-12)
    assert local_position.longitude == pytest.approx(utc_position.longitude, abs=1e-12)
    assert local_position.altitude == pytest.approx(utc_position.altitude, abs=1e-6)


def test_is_ascending_follows_teme_velocity_z_sign():
    ascending_model = _model_with_sgp4(
        _Clock(datetime.datetime(2026, 5, 6, 12, 0, 0)),
        (0, (1.0, 2.0, 3.0), (4.0, 5.0, 1.0)),
    )
    descending_model = _model_with_sgp4(
        _Clock(datetime.datetime(2026, 5, 6, 12, 0, 0)),
        (0, (1.0, 2.0, 3.0), (4.0, 5.0, -1.0)),
    )

    assert ascending_model.is_ascending() is True
    assert descending_model.is_ascending() is False


def test_high_precision_position_path_returns_geodetic_position():
    model = PyOrbitalModel(TLE, None)
    model.high_precision_positions = True

    position = model._compute_position_at(datetime.datetime(2026, 5, 6, 12, 34, 56))

    assert -90.0 <= position.latitude <= 90.0
    assert -180.0 <= position.longitude <= 180.0
    assert 100_000.0 < position.altitude < 2_000_000.0


def test_teme_state_raises_when_clock_returns_none():
    model = PyOrbitalModel(TLE, _Clock(None))

    with pytest.raises(RuntimeError, match="Clock returned None"):
        model.get_position_earth_general_inertial()


def test_teme_state_normalises_aware_clock_to_utc():
    # 14:34:56 at UTC+2 is the same instant as 12:34:56 UTC.
    aware_when = datetime.datetime(
        2026,
        5,
        6,
        14,
        34,
        56,
        tzinfo=datetime.timezone(datetime.timedelta(hours=2)),
    )
    model = _model_with_sgp4(_Clock(aware_when), (0, (1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))

    position = model.get_position_earth_general_inertial()

    assert position == (1.0, 2.0, 3.0)
    expected_jd, expected_fr = jday(2026, 5, 6, 12, 34, 56)
    model.orbital_object.sgp4.assert_called_once_with(expected_jd, expected_fr)


def test_teme_state_raises_on_sgp4_error():
    model = _model_with_sgp4(
        _Clock(datetime.datetime(2026, 5, 6, 12, 0, 0)),
        (2, None, None),
    )

    with pytest.raises(RuntimeError, match="propagation error 2"):
        model.get_velocity_earth_general_inertial()


def test_teme_state_caches_zero_delta_and_recomputes_shifted_epoch():
    when = datetime.datetime(2026, 5, 6, 12, 0, 0)
    clock = _Clock(when)
    model = _model_with_sgp4(clock, (0, (1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))

    assert model.get_position_earth_general_inertial() == (1.0, 2.0, 3.0)
    assert model.get_velocity_earth_general_inertial() == (4.0, 5.0, 6.0)
    assert model.orbital_object.sgp4.call_count == 1

    clock.current = when + datetime.timedelta(seconds=30)
    assert model.get_position_earth_general_inertial() == (1.0, 2.0, 3.0)
    assert model.orbital_object.sgp4.call_count == 2
    assert model.get_velocity_earth_general_inertial() == (4.0, 5.0, 6.0)
    assert model.orbital_object.sgp4.call_count == 2

    assert model.get_position_earth_general_inertial(delta_t=10.0) == (1.0, 2.0, 3.0)
    assert model.orbital_object.sgp4.call_count == 3
    assert model.get_velocity_earth_general_inertial(delta_t=10.0) == (4.0, 5.0, 6.0)
    assert model.orbital_object.sgp4.call_count == 4


@pytest.mark.parametrize("mean_motion_revolution_per_day", [0.0, -0.5])
def test_get_half_cone_angle_rejects_non_positive_mean_motion(
    mean_motion_revolution_per_day,
):
    with pytest.raises(ValueError, match="must be positive"):
        get_half_cone_angle(mean_motion_revolution_per_day)


def test_get_half_cone_angle_shrinks_with_mean_motion_and_elevation():
    mean_motions = [0.5, 1.0, 2.0, 4.0, 8.0, 14.0]
    angles = [get_half_cone_angle(mean_motion) for mean_motion in mean_motions]

    assert all(
        higher_orbit_angle > lower_orbit_angle
        for higher_orbit_angle, lower_orbit_angle in pairwise(angles)
    )
    assert all(math.isfinite(angle) and 0.0 < angle < 80.0 for angle in angles)
    assert get_half_cone_angle(14.0, 10.0) == pytest.approx(
        20.258044366327127, abs=1e-9
    )
    assert get_half_cone_angle(14.0, 10.0) < get_half_cone_angle(14.0, 5.0)


def test_ground_distance_meters_matches_haversine_value():
    first = SpatialPosition(latitude=0.0, longitude=0.0, altitude=1000.0)
    second = SpatialPosition(latitude=0.0, longitude=1.0, altitude=0.0)

    distance_m = _ground_distance_meters(None, first, second)

    assert distance_m == pytest.approx(ONE_DEGREE_GROUND_DISTANCE_KM * 1000.0, abs=1e-6)


def test_ground_distance_km_matches_haversine_value():
    first = SpatialPosition(latitude=0.0, longitude=0.0, altitude=1000.0)
    second = SpatialPosition(latitude=0.0, longitude=1.0, altitude=0.0)

    assert _ground_distance_km(None, first, second) == pytest.approx(
        ONE_DEGREE_GROUND_DISTANCE_KM, abs=1e-12
    )


def test_3d_distance_meters_includes_altitude_difference():
    first = SpatialPosition(latitude=0.0, longitude=0.0, altitude=1000.0)
    second = SpatialPosition(latitude=0.0, longitude=1.0, altitude=0.0)
    expected = math.sqrt((ONE_DEGREE_GROUND_DISTANCE_KM * 1000.0) ** 2 + 1000.0**2)

    assert _3d_distance_meters(None, first, second) == pytest.approx(expected, abs=1e-6)


def test_3d_distance_km_includes_altitude_difference():
    first = SpatialPosition(latitude=0.0, longitude=0.0, altitude=1000.0)
    second = SpatialPosition(latitude=0.0, longitude=1.0, altitude=0.0)
    expected = math.sqrt(ONE_DEGREE_GROUND_DISTANCE_KM**2 + 1.0)

    assert _3d_distance_km(None, first, second) == pytest.approx(expected, abs=1e-12)
    assert _3d_distance_km(None, second, first) == pytest.approx(expected, abs=1e-12)

    # A missing altitude is treated as sea level.
    sea_level = SpatialPosition(latitude=0.0, longitude=0.0)
    assert sea_level.altitude is None
    below = SpatialPosition(latitude=0.0, longitude=1.0, altitude=1000.0)
    assert _3d_distance_km(None, sea_level, below) == pytest.approx(expected, abs=1e-12)


def test_apply_satcom_fix_installs_model_and_distance_patches():
    class FakeDistanceModel:
        pass

    orbital_models = SimpleNamespace()
    generate_satellite = SimpleNamespace(PyOrbitalModel=object())
    distance_models = SimpleNamespace(SkLearningDistanceModel=FakeDistanceModel)
    modules = {
        "sat_com_trajectopy.orbital_models": orbital_models,
        "sat_com_trajectopy.satellites.generate_satellite": generate_satellite,
        "sat_com_trajectopy.distance_models": distance_models,
    }

    with patch("importlib.import_module", side_effect=modules.__getitem__):
        apply_satcom_fix()

    assert orbital_models.PyOrbitalModel is PyOrbitalModel
    assert generate_satellite.PyOrbitalModel is PyOrbitalModel
    assert FakeDistanceModel.get_ground_distance_km is satcom_fix._ground_distance_km
    assert (
        FakeDistanceModel.get_ground_distance_meters
        is satcom_fix._ground_distance_meters
    )
    assert FakeDistanceModel.get_3d_distance_meters is satcom_fix._3d_distance_meters
    assert FakeDistanceModel.get_3d_distance_km is satcom_fix._3d_distance_km


def test_apply_satcom_fix_tolerates_missing_generate_satellite_module():
    class FakeDistanceModel:
        pass

    unpatched_model = object()
    orbital_models = SimpleNamespace()
    generate_satellite = SimpleNamespace(PyOrbitalModel=unpatched_model)
    distance_models = SimpleNamespace(SkLearningDistanceModel=FakeDistanceModel)
    modules = {
        "sat_com_trajectopy.orbital_models": orbital_models,
        "sat_com_trajectopy.distance_models": distance_models,
    }

    def fake_import(name):
        if name == "sat_com_trajectopy.satellites.generate_satellite":
            raise ImportError("generate_satellite is unavailable")
        return modules[name]

    with patch("importlib.import_module", side_effect=fake_import):
        apply_satcom_fix()

    assert orbital_models.PyOrbitalModel is PyOrbitalModel
    assert generate_satellite.PyOrbitalModel is unpatched_model
    assert FakeDistanceModel.get_ground_distance_km is satcom_fix._ground_distance_km
