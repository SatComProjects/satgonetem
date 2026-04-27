"""Geometric utilities for satellite-ground link calculations."""

import math
from typing import Tuple

import numpy as np


def latlonelev_to_xyz(lat: float, lon: float, elev: float) -> Tuple[float, float, float]:
    """Convert geodetic coordinates to Cartesian coordinates.

    Args:
        lat: Latitude in degrees.
        lon: Longitude in degrees.
        elev: Altitude in kilometres.

    Returns:
        A ``(x, y, z)`` tuple in kilometres.
    """
    R = 6378.137  # Earth's radius in km
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    r = R + elev
    x = r * math.cos(lat_rad) * math.cos(lon_rad)
    y = r * math.cos(lat_rad) * math.sin(lon_rad)
    z = r * math.sin(lat_rad)
    return x, y, z


def get_elevation_angle(
    sat_coordinates: Tuple[float, float, float],
    gnd_coordinates: Tuple[float, float, float],
) -> float:
    """Compute the elevation angle from a ground station to a satellite.

    Uses the law of cosines on the Earth-centre / ground-station / satellite
    triangle.  The formula is:

        el = arctan((cos(theta) - R_E / (R_E + h)) / sin(theta))

    where theta is the Earth central angle between the two position vectors
    and h is the satellite altitude above the surface.

    Args:
        sat_coordinates: ``(latitude, longitude, altitude_km)`` of the satellite.
        gnd_coordinates: ``(latitude, longitude, altitude_km)`` of the ground station.

    Returns:
        Elevation angle above the local horizon in degrees.  Positive values
        indicate the satellite is above the horizon; negative values indicate
        it is below.
    """
    R_E = 6378.137
    h = sat_coordinates[2]

    sat_vec = latlonelev_to_xyz(*sat_coordinates)
    gnd_vec = latlonelev_to_xyz(*gnd_coordinates)

    cos_theta = float(
        np.dot(gnd_vec, sat_vec) / (np.linalg.norm(gnd_vec) * np.linalg.norm(sat_vec))
    )
    cos_theta = max(-1.0, min(1.0, cos_theta))
    theta = math.acos(cos_theta)

    sin_theta = math.sin(theta)
    if abs(sin_theta) < 1e-10:
        return 90.0 if theta < math.pi / 2.0 else -90.0

    numerator = cos_theta - R_E / (R_E + h)
    return math.degrees(math.atan(numerator / sin_theta))
