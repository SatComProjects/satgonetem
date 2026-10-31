"""Regression tests for UTIL-07.

``_get_orbital_elements`` used to assume that every movement model exposes an
SGP4 ``Satrec`` with ``nodeo``/``inclo`` attributes.  pyorbital's ``Orbital``
has neither, which made ``add_mesh_links`` crash with ``AttributeError``.

These tests use fake satellites and movement models only: no TLE parsing, no
real propagation and no network access.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from satgonetem.utils.project_builder import _get_orbital_elements


def _teme_state(
    raan_deg: float,
    inc_deg: float,
    arg_lat_deg: float,
    radius_km: float = 7000.0,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return a circular-orbit TEME (r, v) pair with the given elements."""
    raan = math.radians(raan_deg)
    inc = math.radians(inc_deg)
    arg_lat = math.radians(arg_lat_deg)

    x_hat = (math.cos(raan), math.sin(raan), 0.0)
    y_hat = (
        -math.cos(inc) * math.sin(raan),
        math.cos(inc) * math.cos(raan),
        math.sin(inc),
    )

    r = tuple(
        radius_km
        * (math.cos(arg_lat) * x_hat[i] + math.sin(arg_lat) * y_hat[i])
        for i in range(3)
    )
    speed_km_s = math.sqrt(398600.4418 / radius_km)
    v = tuple(
        speed_km_s
        * (-math.sin(arg_lat) * x_hat[i] + math.cos(arg_lat) * y_hat[i])
        for i in range(3)
    )
    return r, v


class _Satellite:
    def __init__(self, movement_model):
        self._movement_model = movement_model

    def get_movement_model(self):
        return self._movement_model


class _Sgp4Model:
    """Fake SGP4 model exposing ``nodeo``/``inclo`` like a ``Satrec``.

    The propagated state deliberately describes a different plane so the test
    fails if the TLE-element path is replaced by state-vector derivation.
    """

    def __init__(self) -> None:
        self.orbital_object = SimpleNamespace(
            nodeo=math.radians(123.4),
            inclo=math.radians(53.0),
        )

    def get_position_earth_general_inertial(self):
        return (7000.0, 0.0, 0.0), (0.0, 7.5, 0.0)

    def get_longitude_latitude_altitude(self):
        return SimpleNamespace(altitude=550_000.0)


class _PyOrbitalModel:
    """Fake pyorbital model: ``Orbital`` without ``nodeo``/``inclo``.

    The stock pyorbital ``Orbital`` exposes only the TEME state through the
    movement model, so RAAN and inclination must be derived from r x v.
    """

    def __init__(self, raan_deg: float, inc_deg: float, arg_lat_deg: float) -> None:
        self._raan_deg = raan_deg
        self._state = _teme_state(raan_deg, inc_deg, arg_lat_deg)
        # pyorbital-like attributes, but neither nodeo nor inclo.
        self.orbital_object = SimpleNamespace(
            inclination=math.radians(inc_deg),
            right_ascension=math.radians(raan_deg),
        )

    def get_position_earth_general_inertial(self):
        return self._state

    def get_longitude_latitude_altitude(self):
        return SimpleNamespace(altitude=550_000.0)


class _UnusableModel:
    """Movement model with no TLE elements and no TEME propagation."""

    def __init__(self) -> None:
        self.orbital_object = object()


def test_sgp4_tle_elements_take_precedence():
    """The old SGP4 path still uses nodeo/inclo from the Satrec."""
    elements = _get_orbital_elements(_Satellite(_Sgp4Model()), None)

    assert elements is not None
    raan, phase, altitude = elements
    assert math.isfinite(raan)
    assert math.isfinite(phase)
    assert math.isfinite(altitude)
    # Derived RAAN would be 0 deg for this state; the TLE says 123.4 deg.
    assert raan == pytest.approx(123.4)
    assert 0.0 <= phase < 360.0
    assert altitude == pytest.approx(550.0)


def test_pyorbital_model_derives_elements_from_teme_state():
    """A model without nodeo/inclo yields finite elements derived from r x v."""
    raan_deg = 210.0
    inc_deg = 60.0
    arg_lat_deg = 25.0
    elements = _get_orbital_elements(
        _Satellite(_PyOrbitalModel(raan_deg, inc_deg, arg_lat_deg)), None
    )

    assert elements is not None
    assert isinstance(elements, tuple)
    assert len(elements) == 3
    raan, phase, altitude = elements
    assert all(math.isfinite(value) for value in elements)
    assert raan == pytest.approx(raan_deg, abs=1e-9)
    assert phase == pytest.approx(arg_lat_deg, abs=1e-9)
    assert altitude == pytest.approx(550.0)


def test_unusable_model_returns_none():
    """Without either element source the documented contract returns None."""
    assert _get_orbital_elements(_Satellite(_UnusableModel()), None) is None
