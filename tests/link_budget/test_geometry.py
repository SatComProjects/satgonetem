"""Tests for satgonetem.link_budget.geometry."""

import math

import pytest

from satgonetem.link_budget.geometry import get_elevation_angle, latlonelev_to_xyz


class TestLatlonelevToXyz:
    def test_equator_prime_meridian(self):
        x, y, z = latlonelev_to_xyz(0.0, 0.0, 0.0)
        assert x == pytest.approx(6378.137)
        assert y == pytest.approx(0.0, abs=1e-9)
        assert z == pytest.approx(0.0, abs=1e-9)

    def test_north_pole(self):
        x, y, z = latlonelev_to_xyz(90.0, 0.0, 0.0)
        assert x == pytest.approx(0.0, abs=1e-9)
        assert y == pytest.approx(0.0, abs=1e-9)
        assert z == pytest.approx(6378.137)

    def test_with_altitude(self):
        x, y, z = latlonelev_to_xyz(0.0, 0.0, 100.0)
        assert x == pytest.approx(6378.137 + 100.0)


class TestGetElevationAngle:
    def test_same_point(self):
        """Satellite directly above ground station -> elevation 90 degrees."""
        angle = get_elevation_angle(
            sat_coordinates=(0.0, 0.0, 550.0),
            gnd_coordinates=(0.0, 0.0, 0.0),
        )
        assert angle == pytest.approx(90.0, abs=1e-6)

    def test_far_apart(self):
        """Satellite on opposite side of Earth -> elevation -90 degrees."""
        angle = get_elevation_angle(
            sat_coordinates=(0.0, 180.0, 550.0),
            gnd_coordinates=(0.0, 0.0, 0.0),
        )
        assert angle == pytest.approx(-90.0, abs=1e-3)

    def test_returns_float(self):
        angle = get_elevation_angle(
            sat_coordinates=(10.0, 20.0, 550.0),
            gnd_coordinates=(0.0, 0.0, 0.0),
        )
        assert isinstance(angle, float)

    def test_known_geometry(self):
        """Validate against an analytically computed case.

        For a satellite at altitude h=550 km directly above the equator at
        lon=20 deg and a ground station at the equator at lon=0 deg, the Earth
        central angle theta equals 20 degrees. The expected elevation angle is:

            el = arctan((cos(20) - R_E / (R_E + 550)) / sin(20))
        """
        import math

        R_E = 6378.137
        h = 550.0
        theta_deg = 20.0
        theta = math.radians(theta_deg)
        expected = math.degrees(
            math.atan((math.cos(theta) - R_E / (R_E + h)) / math.sin(theta))
        )
        angle = get_elevation_angle(
            sat_coordinates=(0.0, 20.0, h),
            gnd_coordinates=(0.0, 0.0, 0.0),
        )
        assert angle == pytest.approx(expected, abs=1e-4)
