"""Propagation loss calculations.

Atmospheric attenuation requires the optional ``itur`` package.
Install it with ``pip install satgonetem[extra]``.
"""

import math
from typing import Tuple

from satgonetem.link_budget.constants import SPEED_OF_LIGHT
from satgonetem.link_budget.conversions import linear_to_db


def calculate_free_space_loss_db(
    frequency_ghz: float,
    distance_km: float,
) -> float:
    """Calculate free-space path loss (FSPL) in dB.

    Args:
        frequency_ghz: Carrier frequency in GHz.
        distance_km: Path length (slant range) in kilometres.

    Returns:
        Free-space loss in dB.
    """
    frequency_hz = frequency_ghz * 1e9
    wavelength = SPEED_OF_LIGHT / frequency_hz
    fspl_linear = ((4.0 * math.pi * distance_km * 1e3) / wavelength) ** 2
    return linear_to_db(fspl_linear)


def calculate_atmospheric_attenuation_dB(
    lat_GS: float,
    lon_GS: float,
    frequency_ghz: float,
    elevation_angle: float,
    unavailability: float,
    antenna_diameter: float,
) -> Tuple[float, float, float, float, float]:
    """Calculate atmospheric attenuation along a slant path using ITU-R models.

    This function wraps :func:`itur.atmospheric_attenuation_slant_path` and
    returns the total attenuation together with the individual contributions.

    Args:
        lat_GS: Ground station latitude in degrees (-90 to 90).
        lon_GS: Ground station longitude in degrees (-180 to 180).
        frequency_ghz: Carrier frequency in GHz (0 to 1000).
        elevation_angle: Elevation angle in degrees (0 to 90).
        unavailability: Time percentage of unavailability in % (0 to 100).
        antenna_diameter: Antenna diameter in metres (0 to 100).

    Returns:
        A tuple ``(total, gaseous, cloud, rain, scintillation)`` in dB.

    Raises:
        ValueError: If any input is outside its valid range.
        RuntimeError: If the optional ``itur`` package is not installed.
    """
    if not (0 <= unavailability <= 100):
        raise ValueError("Unavailability must be between 0 and 100.")
    if not (0 <= elevation_angle <= 90):
        raise ValueError("Elevation angle must be between 0 and 90 degrees.")
    if not (0 <= antenna_diameter <= 100):
        raise ValueError("Antenna diameter must be between 0 and 100 meters.")
    if not (0 <= frequency_ghz <= 1000):
        raise ValueError("Frequency must be between 0 and 1000 GHz.")
    if not (-90 <= lat_GS <= 90):
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    if not (-180 <= lon_GS <= 180):
        raise ValueError("Longitude must be between -180 and 180 degrees.")

    try:
        import itur
        import astropy.units as u
    except ImportError as exc:
        raise RuntimeError(
            "The 'itur' package is required for atmospheric attenuation calculations. "
            "Install it with: pip install satgonetem[extra]"
        ) from exc

    f = frequency_ghz * u.GHz
    D = antenna_diameter * u.m

    a_gaseous, a_cloud, a_rain, a_scintillation, a_total = (
        itur.atmospheric_attenuation_slant_path(
            lat_GS,
            lon_GS,
            f,
            elevation_angle,
            unavailability,
            D,
            return_contributions=True,
        )
    )

    return (
        float(a_total.to(u.dB).value),
        float(a_gaseous.to(u.dB).value),
        float(a_cloud.to(u.dB).value),
        float(a_rain.to(u.dB).value),
        float(a_scintillation.to(u.dB).value),
    )
