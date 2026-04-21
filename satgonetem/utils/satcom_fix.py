import datetime

import numpy as np

from datetime import timezone
from typing import Optional, Tuple

from pydantic import BaseModel
from sgp4.api import Satrec, WGS72, jday, SGP4_ERRORS
from astropy.coordinates import TEME, ITRS, CartesianRepresentation, EarthLocation
from astropy.time import Time
import astropy.units as u

from sat_com_application.time_managers import TimeManager
from sat_com_model.models import MovementModel, SpatialPosition

SGP4_ERRORS = {
    1: "Mean eccentricity is outside the range 0 ≤ e < 1",
    2: "Mean motion has fallen below zero",
    3: "Perturbed eccentricity is outside the range 0 ≤ e ≤ 1",
    4: "Length of the orbit's semi-latus rectum has fallen below zero",
    6: "Orbit has decayed: the computed position is underground.",
}  # From https://pypi.org/project/sgp4/, scroll down to "The possible error codes are"


class PyOrbitalModel(MovementModel):
    """
    Orbital model for computing the position of an orbiting object using TLE elements.

    Uses the SGP4/SDP4 library for propagation and astropy for TEME-to-geodetic conversion.

    Attributes:
        orbital_object: SGP4 satellite record built from TLE.
        clock: Time source used to determine the current epoch.
        last_spatial_position: Cached geodetic position from the last computation.
        last_time_position_calculated: Timestamp of the last cached computation.
    """

    orbital_object: Satrec
    clock: TimeManager

    last_spatial_position: Optional[SpatialPosition] = None
    last_time_position_calculated: Optional[datetime.datetime] = None

    def __init__(self, tle: dict, time_manager: TimeManager):
        """
        Initialize the orbital model from a TLE dictionary.

        Args:
            tle: Dict with keys 'satellite_name', 'line1', and 'line2'.
            time_manager: Provides the current simulation or wall-clock time.
        """
        super().__init__()

        match tle.get("satellite_name"):
            case str(name) if name.strip():
                self.satellite_name: str = name.strip()
            case _:
                raise ValueError("Invalid or missing satellite name in TLE")

        match tle.get("line1"), tle.get("line2"):
            case str(line1), str(line2) if line1.strip() and line2.strip():
                self.line1: str = line1.strip()
                self.line2: str = line2.strip()
            case _:
                raise ValueError("Invalid or missing TLE lines in input")

        self.orbital_object = Satrec.twoline2rv(self.line1, self.line2, WGS72)
        self.clock = time_manager

    def is_ascending(self) -> bool:
        """
        Return True if the satellite is moving northward (ascending arc) in TEME frame.

        Returns:
            bool: True when the TEME z-component of velocity is positive.
        """
        r_teme, v_teme = self.get_position_earth_general_inertial()
        return v_teme[2] > 0

    def _compute_position_at(self, t: datetime.datetime) -> SpatialPosition:
        """
        Compute geodetic position at an explicit UTC datetime.

        Args:
            t: UTC datetime at which to evaluate the satellite position.

        Returns:
            SpatialPosition with longitude (deg), latitude (deg), altitude (m).

        Raises:
            RuntimeError: If SGP4/SDP4 propagation returns a non-zero error code.
        """
        jd, fr = jday(
            t.year,
            t.month,
            t.day,
            t.hour,
            t.minute,
            t.second + t.microsecond * 1e-6,
        )

        error_code, r_teme, v_teme = self.orbital_object.sgp4(jd, fr)
        if error_code != 0:
            msg = SGP4_ERRORS.get(error_code, "Unknown SGP4 error")
            raise RuntimeError(f"SGP4/SDP4 propagation error {error_code}: {msg}")

        obstime = Time(t, scale="utc")
        teme = TEME(
            CartesianRepresentation(
                r_teme[0] * u.km, r_teme[1] * u.km, r_teme[2] * u.km
            ),
            obstime=obstime,
        )
        itrs = teme.transform_to(ITRS(obstime=obstime))

        loc = EarthLocation.from_geocentric(itrs.x, itrs.y, itrs.z)
        lat_deg: float = float(np.real(loc.lat.to_value(u.deg)))
        lon_deg: float = float(np.real(loc.lon.to_value(u.deg)))
        alt_m: float = float(np.real(loc.height.to_value(u.km))) * 1000.0

        lon_deg = ((lon_deg + 180.0) % 360.0) - 180.0

        return SpatialPosition(longitude=lon_deg, latitude=lat_deg, altitude=alt_m)

    def get_longitude_latitude(self) -> SpatialPosition:
        """
        Return geodetic longitude, latitude, and altitude (m) at the clock's current UTC time.

        Caches the result; repeated calls at the same timestamp skip recomputation.

        Returns:
            SpatialPosition with longitude (deg), latitude (deg), altitude (m).

        Raises:
            RuntimeError: If SGP4/SDP4 propagation fails.
        """
        t_now = self.clock.get_current_time()
        if t_now is None:
            raise RuntimeError("Clock returned None for current time")

        if (
            self.last_time_position_calculated is not None
            and t_now == self.last_time_position_calculated
            and self.last_spatial_position is not None
        ):
            return self.last_spatial_position

        new_position = self._compute_position_at(t_now)
        self.last_spatial_position = new_position
        self.last_time_position_calculated = t_now
        return new_position

    def get_longitude_latitude_altitude(self, delta_t: float = 0.0) -> SpatialPosition:
        """
        Return geodetic position at the clock's current time offset by delta_t seconds.

        Args:
            delta_t: Seconds to add to the current clock time. May be negative.

        Returns:
            SpatialPosition with longitude (deg), latitude (deg), altitude (m).

        Raises:
            RuntimeError: If SGP4/SDP4 propagation fails.
        """
        if delta_t == 0.0:
            return self.get_longitude_latitude()
        t_now = self.clock.get_current_time()
        if t_now is None:
            raise RuntimeError("Clock returned None for current time")
        t_target = t_now + datetime.timedelta(seconds=delta_t)
        return self._compute_position_at(t_target)

    def get_position_earth_general_inertial(self) -> Tuple[tuple, tuple]:
        """
        Return TEME-frame position and velocity at the clock's current UTC time.

        Returns:
            Tuple of (r_teme, v_teme), each a 3-tuple (x, y, z).
            Position in kilometers; velocity in kilometers per second.

        Raises:
            RuntimeError: If SGP4/SDP4 propagation fails.
        """
        t = self.clock.get_current_time()
        if t is None:
            raise RuntimeError("Clock returned None for current time")
        if t.tzinfo is None or t.utcoffset() is None:
            t_utc = t
        else:
            t_utc = t.astimezone(timezone.utc)

        jd, fr = jday(
            t_utc.year,
            t_utc.month,
            t_utc.day,
            t_utc.hour,
            t_utc.minute,
            t_utc.second + t_utc.microsecond * 1e-6,
        )

        error_code, r_teme, v_teme = self.orbital_object.sgp4(jd, fr)
        if error_code != 0:
            msg = SGP4_ERRORS.get(error_code, "Unknown SGP4 error")
            raise RuntimeError(f"SGP4/SDP4 propagation error {error_code}: {msg}")

        return r_teme, v_teme


class TwoLineElement(BaseModel):
    """Two-line element set for a single satellite.

    Attributes:
        satellite_name: Human-readable name.
        line1: TLE line 1.
        line2: TLE line 2.
    """

    satellite_name: str
    line1: str
    line2: str


def get_half_cone_angle(
    mean_motion_revolution_per_day: float, min_elevation_deg: float = 10.0
) -> float:
    """
    Return the Earth central angle (degrees) for single-coverage per Beste's star design.

    Uses min_elevation_deg as the minimum ground elevation angle. Derived from the
    law of sines in the satellite-edge-Earth triangle: sin(eta) = R * sin(90+eps) / a,
    and central angle rho = (90 - eps) - eta.

    Args:
        mean_motion_revolution_per_day: Satellite mean motion in revolutions per day.
        min_elevation_deg: Minimum ground elevation angle in degrees.

    Returns:
        float: Earth central angle rho in degrees.

    Raises:
        ValueError: If mean_motion_revolution_per_day is not positive.
    """
    if mean_motion_revolution_per_day <= 0:
        raise ValueError("mean_motion_revolution_per_day must be positive")

    mean_motion_rad_per_second = mean_motion_revolution_per_day * 2.0 * np.pi / 86400.0
    semi_major_axis = (398600.4418 / (mean_motion_rad_per_second**2)) ** (1.0 / 3.0)

    ratio = 6378.137 * np.sin(np.radians(90.0 + min_elevation_deg)) / semi_major_axis
    ratio = float(np.clip(ratio, -1.0, 1.0))

    return float((90.0 - min_elevation_deg) - np.degrees(np.arcsin(ratio)))


def apply_satcom_fix() -> None:
    """
    Monkey-patch the orbital model in sat_com_trajectopy with the local implementation.

    Replaces PyOrbitalModel in sat_com_trajectopy.orbital_models with the version
    defined in this module.
    """
    import importlib

    pkg = importlib.import_module("sat_com_trajectopy.orbital_models")
    setattr(pkg, "PyOrbitalModel", PyOrbitalModel)
