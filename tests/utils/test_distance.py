"""Tests for the 3-D geodetic distance helper."""

import pytest

from satgonetem.utils.utils import distance_3d_km


def test_ten_degree_separation_is_true_chord():
    # The old surface-arc + altitude formula returned ~1240.5 km.
    distance = distance_3d_km(0.0, 0.0, 550.0, 10.0, 0.0, 0.0)

    assert distance == pytest.approx(1281.509, abs=0.01)


def test_coincident_points_have_zero_distance():
    assert distance_3d_km(12.0, 34.0, 0.5, 12.0, 34.0, 0.5) == pytest.approx(0.0)


def test_vertical_separation_is_preserved():
    assert distance_3d_km(45.0, 10.0, 0.0, 45.0, 10.0, 2.0) == pytest.approx(2.0)


def test_antipodal_surface_points_are_earth_diameter_apart():
    assert distance_3d_km(0.0, 0.0, 0.0, 0.0, 180.0, 0.0) == pytest.approx(
        12742.0
    )
