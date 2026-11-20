import datetime
import math

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

# WGS-84 ellipsoid constants (kilometres).
WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_B = WGS84_A * (1 - WGS84_F)
WGS84_E2 = 1 - (WGS84_B * WGS84_B) / (WGS84_A * WGS84_A)
WGS84_EP2 = (WGS84_A * WGS84_A) / (WGS84_B * WGS84_B) - 1

# Earth radius used by sat_com_trajectopy's sklearn distance model.
EARTH_RADIUS_KM = 6371.009

SGP4_ERRORS = {
    1: "Mean eccentricity is outside the range 0 ≤ e < 1",
    2: "Mean motion has fallen below zero",
    3: "Perturbed eccentricity is outside the range 0 ≤ e ≤ 1",
    4: "Length of the orbit's semi-latus rectum has fallen below zero",
    6: "Orbit has decayed: the computed position is underground.",
}  # From https://pypi.org/project/sgp4/, scroll down to "The possible error codes are"


def teme_to_geodetic(
    r_teme_km: Tuple[float, float, float],
    jd: float,
    fr: float,
) -> Tuple[float, float, float]:
    """Convert a TEME position to WGS-84 geodetic coordinates.

    Uses the standard Vallado/Bowring approach: rotate the TEME vector into an
    Earth-fixed frame with GMST, then solve for latitude/altitude on the WGS-84
    ellipsoid. Polar motion and the UT1-UTC offset are ignored; the resulting
    error is well below one millisecond of propagation delay.

    Args:
        r_teme_km: TEME position vector in kilometres.
        jd: Julian day number (integer part).
        fr: Fractional Julian day.

    Returns:
        Tuple of (latitude degrees, longitude degrees, altitude metres).
    """
    # GMST from the IAU-82 expression, in seconds of time.
    tut1 = (jd + fr - 2451545.0) / 36525.0
    theta = (
        -6.2e-6 * tut1**3
        + 0.093104 * tut1**2
        + (876600.0 * 3600 + 8640184.812866) * tut1
        + 67310.54841
    )
    theta = math.radians(theta / 240.0) % (2 * math.pi)

    x, y, z = r_teme_km
    xe = math.cos(theta) * x + math.sin(theta) * y
    ye = -math.sin(theta) * x + math.cos(theta) * y
    ze = z

    p = math.hypot(xe, ye)
    th = math.atan2(ze * WGS84_A, p * WGS84_B)
    lat = math.atan2(
        ze + WGS84_EP2 * WGS84_B * math.sin(th) ** 3,
        p - WGS84_E2 * WGS84_A * math.cos(th) ** 3,
    )
    lon = math.atan2(ye, xe)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    alt_km = p / math.cos(lat) - n

    return math.degrees(lat), math.degrees(lon), alt_km * 1000.0


class PyOrbitalModel(MovementModel):
    """
    Orbital model for computing the position of an orbiting object using TLE elements.

    Uses the SGP4/SDP4 library for propagation. TEME-to-geodetic conversion uses
    a direct GMST + WGS-84 solution by default; set ``high_precision_positions``
    to True to use the slower Astropy IERS path instead.

    Attributes:
        orbital_object: SGP4 satellite record built from TLE.
        clock: Time source used to determine the current epoch.
        last_spatial_position: Cached geodetic position from the last computation.
        last_time_position_calculated: Timestamp of the last cached computation.
        high_precision_positions: Use Astropy's TEME/ITRS transform when True.
    """

    high_precision_positions: bool = False

    orbital_object: Satrec
    clock: TimeManager

    last_spatial_position: Optional[SpatialPosition] = None
    last_time_position_calculated: Optional[datetime.datetime] = None

    _last_teme_time: Optional[datetime.datetime] = None
    _last_teme_state: Optional[Tuple[tuple, tuple]] = None

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
        return self.get_velocity_earth_general_inertial()[2] > 0

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
        if t.tzinfo is not None and t.utcoffset() is not None:
            t = t.astimezone(timezone.utc)

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

        if self.high_precision_positions:
            return self._compute_position_astropy(t, r_teme)

        lat_deg, lon_deg, alt_m = teme_to_geodetic(r_teme, jd, fr)
        return SpatialPosition(longitude=lon_deg, latitude=lat_deg, altitude=alt_m)

    def _compute_position_astropy(self, t: datetime.datetime, r_teme) -> SpatialPosition:
        """Compute the geodetic position with Astropy's IERS-aware transform.

        Kept as a reference implementation for regression tests and for users
        that need polar motion and UT1-UTC corrections.

        Args:
            t: UTC datetime at which to evaluate the position.
            r_teme: TEME position vector in kilometres.

        Returns:
            SpatialPosition with longitude (deg), latitude (deg), altitude (m).
        """
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

    def get_datetime(self) -> datetime.datetime:
        """
        Return the clock's current time (required by sat_com_topology 5's MovementModel).

        Returns:
            datetime.datetime: The current simulation or wall-clock time.
        """
        return self.clock.get_current_time()

    def _teme_state(self, delta_t: float = 0.0) -> Tuple[tuple, tuple]:
        """
        Return TEME-frame position and velocity at the clock's current UTC time plus delta_t.

        The current-time state (delta_t == 0) is cached, so that a position and a velocity
        requested for the same instant cost a single SGP4 propagation.

        Args:
            delta_t: Seconds to add to the current clock time. May be negative.

        Returns:
            Tuple of (r_teme, v_teme), each a 3-tuple (x, y, z).
            Position in kilometers; velocity in kilometers per second.

        Raises:
            RuntimeError: If the clock returns None or SGP4/SDP4 propagation fails.
        """
        t = self.clock.get_current_time()
        if t is None:
            raise RuntimeError("Clock returned None for current time")
        if delta_t == 0.0 and self._last_teme_time == t and self._last_teme_state is not None:
            return self._last_teme_state

        t_target = t + datetime.timedelta(seconds=delta_t) if delta_t else t
        if t_target.tzinfo is None or t_target.utcoffset() is None:
            t_utc = t_target
        else:
            t_utc = t_target.astimezone(timezone.utc)

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

        if delta_t == 0.0:
            self._last_teme_time = t
            self._last_teme_state = (r_teme, v_teme)
        return r_teme, v_teme

    def get_position_earth_general_inertial(self, delta_t: float = 0.0) -> tuple:
        """
        Return the TEME-frame position at the clock's current UTC time plus delta_t.

        Position only, as MovementModel declares it (sat_com_topology 4 and 5); the velocity
        is get_velocity_earth_general_inertial.

        Args:
            delta_t: Seconds to add to the current clock time. May be negative.

        Returns:
            3-tuple (x, y, z) in kilometers.

        Raises:
            RuntimeError: If SGP4/SDP4 propagation fails.
        """
        return self._teme_state(delta_t)[0]

    def get_velocity_earth_general_inertial(self, delta_t: float = 0.0) -> tuple:
        """
        Return the TEME-frame velocity at the clock's current UTC time plus delta_t
        (required by sat_com_topology 5's MovementModel).

        Args:
            delta_t: Seconds to add to the current clock time. May be negative.

        Returns:
            3-tuple (vx, vy, vz) in kilometers per second.

        Raises:
            RuntimeError: If SGP4/SDP4 propagation fails.
        """
        return self._teme_state(delta_t)[1]


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


def _haversine_ground_distance_km(position_1, position_2) -> float:
    """Great-circle distance in kilometres between two spatial positions."""
    lat1, lon1, lat2, lon2 = map(
        math.radians,
        (
            position_1.latitude,
            position_1.longitude,
            position_2.latitude,
            position_2.longitude,
        ),
    )
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def _ground_distance_km(self, spatial_position_1, spatial_position_2) -> float:
    return _haversine_ground_distance_km(spatial_position_1, spatial_position_2)


def _ground_distance_meters(self, spatial_position_1, spatial_position_2) -> float:
    return _haversine_ground_distance_km(spatial_position_1, spatial_position_2) * 1000.0


def _3d_distance_meters(self, spatial_position_1, spatial_position_2) -> float:
    ground_m = _haversine_ground_distance_km(spatial_position_1, spatial_position_2) * 1000.0
    alt_diff_m = (spatial_position_1.altitude or 0) - (spatial_position_2.altitude or 0)
    return math.sqrt(ground_m**2 + alt_diff_m**2)


def _3d_distance_km(self, spatial_position_1, spatial_position_2) -> float:
    ground_km = _haversine_ground_distance_km(spatial_position_1, spatial_position_2)
    alt_diff_km = ((spatial_position_1.altitude or 0) - (spatial_position_2.altitude or 0)) / 1000.0
    return math.sqrt(ground_km**2 + alt_diff_km**2)


def apply_satcom_fix() -> None:
    """
    Monkey-patch sat_com_trajectopy with the local implementations.

    Replaces PyOrbitalModel in sat_com_trajectopy.orbital_models with the
    version defined in this module, and installs direct-haversine distance
    methods on the default SkLearningDistanceModel. The scalar haversine is
    mathematically identical to sklearn's pairwise implementation but avoids
    its per-call validation overhead.

    Modules that imported PyOrbitalModel before this function ran keep the old
    binding in their module globals. The most important one is
    ``sat_com_trajectopy.satellites.generate_satellite``, which does
    ``from ...orbital_models import PyOrbitalModel`` at import time. Rebind
    those references too, so the patch does not depend on import order.
    """
    import importlib

    orbital_models = importlib.import_module("sat_com_trajectopy.orbital_models")
    setattr(orbital_models, "PyOrbitalModel", PyOrbitalModel)

    try:
        generate_satellite = importlib.import_module(
            "sat_com_trajectopy.satellites.generate_satellite"
        )
    except ImportError:
        pass
    else:
        setattr(generate_satellite, "PyOrbitalModel", PyOrbitalModel)

    distance_models = importlib.import_module("sat_com_trajectopy.distance_models")
    model_class = distance_models.SkLearningDistanceModel
    model_class.get_ground_distance_km = _ground_distance_km
    model_class.get_ground_distance_meters = _ground_distance_meters
    model_class.get_3d_distance_meters = _3d_distance_meters
    model_class.get_3d_distance_km = _3d_distance_km
