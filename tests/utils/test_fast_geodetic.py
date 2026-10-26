"""Regression tests for the fast TEME -> geodetic conversion.

The fast path replaces Astropy's IERS-aware TEME/ITRS transform with a direct
GMST rotation and WGS-84 ellipsoid solution. These tests bound the resulting
error against the Astropy reference path.
"""

from __future__ import annotations

import datetime
import math
import random

import astropy.units as u
import pytest

from astropy.coordinates import TEME, ITRS, CartesianRepresentation, EarthLocation
from astropy.time import Time
from sat_com_model.models import SpatialPosition
from sat_com_trajectopy.distance_models import SkLearningDistanceModel
from sgp4.api import Satrec, WGS72, jday
from sklearn.metrics.pairwise import haversine_distances

from satgonetem.utils.satcom_fix import (
    PyOrbitalModel,
    apply_satcom_fix,
    teme_to_geodetic,
)

TLE_LINE_1 = "1 44057U 19010A   26126.83484127  .00000046  00000+0  86073-4 0  9992"
TLE_LINE_2 = "2 44057  87.9096 236.9957 0002389 103.2080 256.9318 13.16595320346123"
TLE = {
    "satellite_name": "ONEWEB-0012",
    "line1": TLE_LINE_1,
    "line2": TLE_LINE_2,
}


def _astropy_geodetic(
    r_teme, when: datetime.datetime
) -> tuple[float, float, float]:
    """Return (lat deg, lon deg, alt m) using the Astropy reference path."""
    obstime = Time(when, scale="utc")
    teme = TEME(
        CartesianRepresentation(r_teme[0] * u.km, r_teme[1] * u.km, r_teme[2] * u.km),
        obstime=obstime,
    )
    itrs = teme.transform_to(ITRS(obstime=obstime))
    loc = EarthLocation.from_geocentric(itrs.x, itrs.y, itrs.z)
    lat = float(loc.lat.to_value(u.deg))
    lon = float(loc.lon.to_value(u.deg))
    alt = float(loc.height.to_value(u.km)) * 1000.0
    return lat, lon, alt


def test_teme_to_geodetic_matches_astropy():
    """Fast conversion must stay within the documented error budget."""
    satrec = Satrec.twoline2rv(TLE_LINE_1, TLE_LINE_2, WGS72)
    rng = random.Random(42)
    start = datetime.datetime(2026, 5, 6, tzinfo=datetime.timezone.utc)

    max_lat_err = 0.0
    max_lon_err = 0.0
    max_alt_err = 0.0

    for _ in range(200):
        when = start + datetime.timedelta(seconds=rng.uniform(0, 86400))
        jd, fr = jday(
            when.year,
            when.month,
            when.day,
            when.hour,
            when.minute,
            when.second + when.microsecond * 1e-6,
        )
        error_code, r_teme, _ = satrec.sgp4(jd, fr)
        assert error_code == 0

        lat, lon, alt = teme_to_geodetic(r_teme, jd, fr)
        ref_lat, ref_lon, ref_alt = _astropy_geodetic(r_teme, when)

        max_lat_err = max(max_lat_err, abs(lat - ref_lat))
        max_lon_err = max(max_lon_err, abs(((lon - ref_lon + 180.0) % 360.0) - 180.0))
        max_alt_err = max(max_alt_err, abs(alt - ref_alt))

    assert max_lat_err < 2e-3
    assert max_lon_err < 5e-3
    assert max_alt_err < 1.0


def test_orbital_model_fast_and_reference_paths_agree():
    """PyOrbitalModel's default path must agree with its Astropy path."""
    model = PyOrbitalModel(TLE, None)
    when = datetime.datetime(2026, 5, 6, 12, 34, 56, tzinfo=datetime.timezone.utc)

    fast = model._compute_position_at(when)
    model.high_precision_positions = True
    reference = model._compute_position_at(when)

    assert fast.latitude == pytest.approx(reference.latitude, abs=2e-3)
    assert fast.longitude == pytest.approx(reference.longitude, abs=5e-3)
    assert fast.altitude == pytest.approx(reference.altitude, abs=1.0)


def test_distance_model_patch_matches_sklearn():
    """Patched scalar haversine must match sklearn's pairwise implementation."""
    apply_satcom_fix()
    model = SkLearningDistanceModel()
    rng = random.Random(7)

    for _ in range(100):
        position_1 = SpatialPosition(
            latitude=rng.uniform(-80.0, 80.0),
            longitude=rng.uniform(-180.0, 180.0),
            altitude=rng.uniform(0.0, 2000.0) * 1000.0,
        )
        position_2 = SpatialPosition(
            latitude=rng.uniform(-80.0, 80.0),
            longitude=rng.uniform(-180.0, 180.0),
            altitude=rng.uniform(0.0, 2000.0) * 1000.0,
        )

        reference_ground_km = (
            haversine_distances(
                [
                    [math.radians(position_1.latitude), math.radians(position_1.longitude)],
                    [math.radians(position_2.latitude), math.radians(position_2.longitude)],
                ]
            )[0, 1]
            * 6371.009
        )
        reference_3d_m = math.sqrt(
            (reference_ground_km * 1000.0) ** 2
            + (position_1.altitude - position_2.altitude) ** 2
        )

        assert model.get_ground_distance_km(position_1, position_2) == pytest.approx(
            reference_ground_km, abs=1e-6
        )
        assert model.get_3d_distance_meters(position_1, position_2) == pytest.approx(
            reference_3d_m, abs=1e-4
        )
