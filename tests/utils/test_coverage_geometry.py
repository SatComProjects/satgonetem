"""Characterization tests for :mod:`satgonetem.utils.coverage`.

The module exposes one visibility-ring helper and two coverage estimators.
These tests pin the ring geometry, the documented empty-constellation result,
and the agreement between the planar-polygon and spherical estimators on
non-polar geometries.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from satgonetem.utils.coverage import (
    coverage_percentage,
    coverage_percentage_fast,
    coverage_ring_from_elevation,
)

pytestmark = pytest.mark.unit

# Earth-central angle for a 550 km altitude and a 10 deg elevation mask,
# computed from the law of cosines used by the implementation.
_PSI_10_DEG_550_KM = 14.96758061913976
_PSI_30_DEG_550_KM = 7.136086783824261


def _sat(latitude: float, longitude: float, altitude: float = 550.0):
    """Minimal stand-in for a satellite object with a ``position`` mapping."""
    return SimpleNamespace(
        position={
            "latitude": latitude,
            "longitude": longitude,
            "altitude": altitude,
        }
    )


@pytest.mark.parametrize("n_pts", [8, 36, 180])
def test_ring_has_one_point_per_requested_bearing(n_pts: int) -> None:
    lon_deg, lat_deg, polar_flag = coverage_ring_from_elevation(
        lat0_deg=0.0,
        lon0_deg=0.0,
        alt_km=550.0,
        elev_min_deg=10.0,
        n_pts=n_pts,
    )

    assert isinstance(lon_deg, np.ndarray)
    assert isinstance(lat_deg, np.ndarray)
    assert lon_deg.shape == (n_pts,)
    assert lat_deg.shape == (n_pts,)
    assert polar_flag is False


def test_ring_is_centred_on_subpoint_and_spans_visibility_angle() -> None:
    lon_deg, lat_deg, _ = coverage_ring_from_elevation(0.0, 0.0, 550.0, 10.0, 72)

    assert float(lat_deg.max()) == pytest.approx(_PSI_10_DEG_550_KM, abs=1e-9)
    assert float(lat_deg.min()) == pytest.approx(-_PSI_10_DEG_550_KM, abs=1e-9)
    assert float(lon_deg.max()) == pytest.approx(_PSI_10_DEG_550_KM, abs=1e-9)
    assert float(lon_deg.min()) == pytest.approx(-_PSI_10_DEG_550_KM, abs=1e-9)
    assert float(np.mean(lat_deg)) == pytest.approx(0.0, abs=1e-9)
    assert float(np.mean(lon_deg)) == pytest.approx(0.0, abs=1e-9)


def test_ring_longitudes_are_offset_by_subpoint_longitude() -> None:
    lon_deg, _, _ = coverage_ring_from_elevation(0.0, 45.0, 550.0, 10.0, 72)

    assert float(np.mean(lon_deg)) == pytest.approx(45.0, abs=1e-9)
    assert float(lon_deg.max()) == pytest.approx(45.0 + _PSI_10_DEG_550_KM, abs=1e-9)
    assert float(lon_deg.min()) == pytest.approx(45.0 - _PSI_10_DEG_550_KM, abs=1e-9)


def test_ring_latitude_decreases_monotonically_from_north_to_south_bearing() -> None:
    _, lat_deg, _ = coverage_ring_from_elevation(45.0, 0.0, 550.0, 10.0, 36)

    # Bearings run 0..2*pi; the first half sweeps from due north to due south.
    north_to_south = lat_deg[: len(lat_deg) // 2 + 1]

    assert np.all(np.diff(north_to_south) < 0.0)
    assert float(lat_deg[0]) == pytest.approx(45.0 + _PSI_10_DEG_550_KM, abs=1e-9)
    assert float(lat_deg[len(lat_deg) // 2]) == pytest.approx(
        45.0 - _PSI_10_DEG_550_KM, abs=1e-9
    )


def test_higher_elevation_mask_shrinks_the_ring() -> None:
    _, lat_10, _ = coverage_ring_from_elevation(0.0, 0.0, 550.0, 10.0, 36)
    _, lat_30, _ = coverage_ring_from_elevation(0.0, 0.0, 550.0, 30.0, 36)

    assert float(lat_30.max()) == pytest.approx(_PSI_30_DEG_550_KM, abs=1e-9)
    assert float(lat_30.max()) < float(lat_10.max())
    assert float(lat_30.min()) > float(lat_10.min())


def test_ring_is_none_when_altitude_is_below_visible_horizon() -> None:
    lon_deg, lat_deg, polar_flag = coverage_ring_from_elevation(
        0.0, 0.0, -1000.0, elev_min_deg=10.0
    )

    assert lon_deg is None
    assert lat_deg is None
    assert polar_flag is False


def test_zero_altitude_zenith_mask_gives_degenerate_ring() -> None:
    lon_deg, lat_deg, polar_flag = coverage_ring_from_elevation(
        0.0, 0.0, 0.0, elev_min_deg=90.0, n_pts=8
    )

    # The visibility angle is zero, so every ring point collapses onto the
    # subpoint instead of returning the documented (None, None, False).
    assert np.allclose(lon_deg, 0.0)
    assert np.allclose(lat_deg, 0.0)
    assert polar_flag is False


def test_ring_crossing_a_pole_is_flagged_and_extended() -> None:
    lon_deg, lat_deg, polar_flag = coverage_ring_from_elevation(
        80.0, 0.0, 550.0, 10.0, 36
    )

    assert polar_flag is True
    assert lon_deg.shape == (40,)
    assert lat_deg.shape == (40,)
    assert np.any(lon_deg < -180.0)
    assert np.any(lon_deg > 180.0)
    # Closure uses the geodetic pole, never an invalid +/-180 latitude.
    assert np.all(lat_deg >= -90.0)
    assert np.all(lat_deg <= 90.0)
    assert not np.any(np.isclose(np.abs(lat_deg), 180.0))
    assert np.any(np.isclose(lat_deg, 90.0))


def test_ring_at_exact_north_pole_is_closed_and_valid() -> None:
    lon_deg, lat_deg, polar_flag = coverage_ring_from_elevation(
        90.0, 0.0, 550.0, 10.0, 72
    )

    assert polar_flag is True
    assert np.all(np.isfinite(lon_deg))
    assert np.all(np.isfinite(lat_deg))
    assert np.all(lat_deg >= 75.0 - 1e-6)
    assert np.all(lat_deg <= 90.0)
    assert np.any(np.isclose(lat_deg, 90.0))
    assert float(lon_deg.max() - lon_deg.min()) > 300.0


@pytest.mark.parametrize(
    ("lat0", "elev"),
    [
        (0.0, 10.0),
        (0.0, 30.0),
        (45.0, 10.0),
        (45.0, 30.0),
    ],
)
def test_polygon_and_fast_coverage_agree_for_non_polar_satellites(
    lat0: float, elev: float
) -> None:
    sats = [_sat(lat0, 0.0)]

    polygon = coverage_percentage(sats, elev_min_deg=elev, n_ring_pts=360)
    fast = coverage_percentage_fast(sats, elev_min_deg=elev)

    assert 0.0 <= polygon <= 100.0
    assert 0.0 <= fast <= 100.0
    assert polygon == pytest.approx(fast, abs=0.5)


def test_polygon_and_fast_coverage_agree_for_small_constellation() -> None:
    sats = [
        _sat(0.0, 0.0),
        _sat(30.0, 90.0),
        _sat(-30.0, 180.0),
        _sat(60.0, -90.0),
    ]

    for elev in (10.0, 30.0):
        polygon = coverage_percentage(sats, elev_min_deg=elev, n_ring_pts=360)
        fast = coverage_percentage_fast(sats, elev_min_deg=elev)

        assert 0.0 <= polygon <= 100.0
        assert 0.0 <= fast <= 100.0
        assert polygon == pytest.approx(fast, abs=0.5)


def test_coverage_agrees_with_coarser_grid_and_limited_latitude_band() -> None:
    sats = [
        _sat(0.0, 0.0),
        _sat(30.0, 90.0),
        _sat(-30.0, 180.0),
        _sat(60.0, -90.0),
    ]

    polygon = coverage_percentage(
        sats,
        elev_min_deg=10.0,
        n_ring_pts=360,
        grid_res_deg=2.0,
        max_latitude_deg=60.0,
    )
    fast = coverage_percentage_fast(
        sats,
        elev_min_deg=10.0,
        grid_res_deg=2.0,
        max_latitude_deg=60.0,
    )

    assert 0.0 <= polygon <= 100.0
    assert 0.0 <= fast <= 100.0
    assert polygon == pytest.approx(fast, abs=0.5)


@pytest.mark.parametrize("elev", [0.0, 10.0, 25.0, 45.0])
def test_coverage_values_are_percentages(elev: float) -> None:
    sats = [_sat(0.0, 0.0), _sat(45.0, 120.0)]

    polygon = coverage_percentage(sats, elev_min_deg=elev, n_ring_pts=360)
    fast = coverage_percentage_fast(sats, elev_min_deg=elev)

    assert 0.0 <= polygon <= 100.0
    assert 0.0 <= fast <= 100.0


def test_empty_constellation_has_zero_coverage() -> None:
    assert coverage_percentage([]) == 0.0
    assert coverage_percentage_fast([]) == 0.0


def test_satellite_below_minimum_altitude_contributes_zero_coverage() -> None:
    sats = [_sat(0.0, 0.0, altitude=-1000.0)]

    assert coverage_percentage(sats, elev_min_deg=10.0, n_ring_pts=360) == 0.0
    assert coverage_percentage_fast(sats, elev_min_deg=10.0) == 0.0


def test_polygon_and_fast_coverage_agree_at_the_pole() -> None:
    sats = [_sat(90.0, 0.0)]

    polygon = coverage_percentage(sats, elev_min_deg=10.0, n_ring_pts=360)
    fast = coverage_percentage_fast(sats, elev_min_deg=10.0)

    assert polygon == pytest.approx(fast, abs=1.0)
