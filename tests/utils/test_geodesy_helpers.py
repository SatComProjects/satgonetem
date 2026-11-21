"""Characterization tests for the experimental geodesy helpers.

The helpers live in :mod:`satgonetem.utils.test`, which ``docs/utils.md``
describes as a scratch geometry module rather than a test suite. Angles are in
radians unless a function name says otherwise.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pytest

from satgonetem.utils.test import (
    ecef_to_eci,
    geodetic_to_ecef,
    geodetic_to_orbital,
    geodetic_to_orbital_sgp4,
    gmst_from_jd,
    julian_date,
)

pytestmark = pytest.mark.unit

_WGS84_A_KM = 6378.137
_WGS84_B_KM = 6356.752314245179
_J2000_JD = 2451545.0
_J2000_GMST_RAD = 4.894961212735793


def test_geodetic_to_ecef_returns_three_element_float_array() -> None:
    result = geodetic_to_ecef(0.0, 0.0, 0.0)

    assert isinstance(result, np.ndarray)
    assert result.shape == (3,)
    assert np.issubdtype(result.dtype, np.floating)


def test_geodetic_to_ecef_at_equator_and_prime_meridian() -> None:
    x, y, z = geodetic_to_ecef(0.0, 0.0, 0.0)

    assert float(x) == pytest.approx(_WGS84_A_KM, abs=1e-9)
    assert float(y) == pytest.approx(0.0, abs=1e-9)
    assert float(z) == pytest.approx(0.0, abs=1e-9)


def test_geodetic_to_ecef_at_equator_and_ninety_degrees_east() -> None:
    x, y, _ = geodetic_to_ecef(0.0, math.pi / 2.0, 0.0)

    assert float(x) == pytest.approx(0.0, abs=1e-6)
    assert float(y) == pytest.approx(_WGS84_A_KM, abs=1e-6)


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_geodetic_to_ecef_at_geographic_poles(sign: float) -> None:
    x, y, z = geodetic_to_ecef(sign * math.pi / 2.0, 0.0, 0.0)

    assert float(x) == pytest.approx(0.0, abs=1e-6)
    assert float(y) == pytest.approx(0.0, abs=1e-6)
    assert float(z) == pytest.approx(sign * _WGS84_B_KM, abs=1e-6)


def test_geodetic_to_ecef_altitude_extends_radius_at_equator() -> None:
    x, y, z = geodetic_to_ecef(0.0, 0.0, 100.0)

    assert float(x) == pytest.approx(_WGS84_A_KM + 100.0, abs=1e-9)
    assert float(y) == pytest.approx(0.0, abs=1e-9)
    assert float(z) == pytest.approx(0.0, abs=1e-9)


def test_gmst_from_jd_at_j2000() -> None:
    gmst = gmst_from_jd(_J2000_JD)

    assert float(gmst) == pytest.approx(_J2000_GMST_RAD, abs=1e-12)
    assert float(gmst) == pytest.approx(math.radians(280.46061837), abs=1e-12)


@pytest.mark.parametrize("jd", [_J2000_JD, 2451545.5, 2460837.0, 2_400_000.25])
def test_gmst_from_jd_is_reduced_to_one_turn(jd: float) -> None:
    gmst = float(gmst_from_jd(jd))

    assert 0.0 <= gmst < 2.0 * math.pi


def test_gmst_from_jd_advances_by_sidereal_rate() -> None:
    one_hour = 1.0 / 24.0
    delta = float(gmst_from_jd(_J2000_JD + one_hour)) - float(
        gmst_from_jd(_J2000_JD)
    )

    assert delta == pytest.approx(math.radians(360.98564736629 / 24.0), abs=1e-9)


def test_ecef_to_eci_is_identity_at_zero_gmst() -> None:
    ecef = np.array([1.0, 2.0, 3.0])

    eci = ecef_to_eci(ecef, 0.0)

    assert isinstance(eci, np.ndarray)
    assert eci == pytest.approx([1.0, 2.0, 3.0], abs=1e-12)


def test_ecef_to_eci_applies_quarter_turn_at_ninety_degrees() -> None:
    eci_x = ecef_to_eci(np.array([1.0, 0.0, 0.0]), math.pi / 2.0)
    eci_y = ecef_to_eci(np.array([0.0, 1.0, 0.0]), math.pi / 2.0)
    eci_z = ecef_to_eci(np.array([0.0, 0.0, 4.0]), math.pi / 2.0)

    assert eci_x == pytest.approx([0.0, -1.0, 0.0], abs=1e-12)
    assert eci_y == pytest.approx([1.0, 0.0, 0.0], abs=1e-12)
    assert eci_z == pytest.approx([0.0, 0.0, 4.0], abs=1e-12)


@pytest.mark.parametrize("gmst", [0.0, 0.7, math.pi / 2.0, math.pi, 2.0 * math.pi])
def test_ecef_to_eci_preserves_vector_norm(gmst: float) -> None:
    ecef = np.array([1234.0, -5678.0, 910.0])

    eci = ecef_to_eci(ecef, gmst)

    assert float(np.linalg.norm(eci)) == pytest.approx(
        float(np.linalg.norm(ecef)), rel=1e-12
    )


def test_julian_date_at_j2000_epoch() -> None:
    epoch = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert float(julian_date(epoch)) == pytest.approx(_J2000_JD)


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc), 2451544.5),
        (datetime(2025, 6, 10, 12, 0, 0, tzinfo=timezone.utc), 2460837.0),
        (datetime(2025, 6, 10, 6, 0, 0, tzinfo=timezone.utc), 2460836.75),
    ],
)
def test_julian_date_for_known_utc_datetimes(
    epoch: datetime, expected: float
) -> None:
    assert float(julian_date(epoch)) == pytest.approx(expected)


def test_julian_date_ignores_tzinfo_metadata() -> None:
    aware = datetime(2025, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2025, 6, 10, 12, 0, 0)

    assert float(julian_date(aware)) == float(julian_date(naive))


def test_geodetic_to_orbital_returns_documented_keys() -> None:
    result = geodetic_to_orbital(10.0, 45.0, 500.0, 45.0)

    assert set(result) == {
        "inclination_deg",
        "mean_motion_rev_per_day",
        "argument_of_perigee_deg",
        "mean_anomaly_deg",
        "raan_deg",
    }
    assert result["inclination_deg"] == pytest.approx(45.0)
    assert result["mean_motion_rev_per_day"] == pytest.approx(1.0)
    assert result["argument_of_perigee_deg"] == pytest.approx(0.0)
    assert result["mean_anomaly_deg"] == pytest.approx(0.0)
    assert result["raan_deg"] == pytest.approx(0.0)


def test_geodetic_to_orbital_ignores_position_arguments() -> None:
    # The implementation echoes only the inclination and constant placeholders;
    # latitude, longitude, altitude, and epoch do not affect the result.
    at_equator = geodetic_to_orbital(0.0, 0.0, 0.0, 45.0)
    at_off_nadir = geodetic_to_orbital(80.0, 120.0, 1000.0, 45.0)

    assert at_equator == at_off_nadir


def test_geodetic_to_orbital_sgp4_returns_keys_and_finite_values() -> None:
    epoch = datetime(2025, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

    result = geodetic_to_orbital_sgp4(10.0, 45.0, 500.0, 45.0, epoch)

    assert set(result) == {
        "mean_motion_rev_per_day",
        "raan_rad",
        "arg_perigee_rad",
        "mean_anomaly_rad",
    }
    assert all(math.isfinite(float(value)) for value in result.values())


def test_geodetic_to_orbital_sgp4_circular_leo_mean_motion() -> None:
    epoch = datetime(2025, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

    result = geodetic_to_orbital_sgp4(10.0, 45.0, 500.0, 45.0, epoch)

    assert float(result["mean_motion_rev_per_day"]) == pytest.approx(
        15.221489003059029, abs=1e-9
    )
    assert 0.0 <= float(result["raan_rad"]) < 2.0 * math.pi
    assert float(result["arg_perigee_rad"]) == pytest.approx(0.0)
    assert 0.0 <= float(result["mean_anomaly_rad"]) < 2.0 * math.pi


def test_geodetic_to_orbital_sgp4_uses_zero_raan_over_the_pole() -> None:
    epoch = datetime(2025, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

    result = geodetic_to_orbital_sgp4(90.0, 0.0, 500.0, 90.0, epoch)

    assert float(result["raan_rad"]) == pytest.approx(0.0)
    assert float(result["mean_anomaly_rad"]) == pytest.approx(math.pi / 2.0)
