from datetime import datetime, timezone

import numpy as np


def geodetic_to_orbital(lat_deg, lon_deg, alt_km, incl_deg, epoch=None):
    """Convert geodetic coordinates to circular Keplerian orbital elements (degrees).

    Args:
        lat_deg: Geodetic latitude in degrees.
        lon_deg: Geodetic longitude in degrees.
        alt_km: Altitude above the WGS-84 ellipsoid in kilometres.
        incl_deg: Orbital inclination in degrees (non-zero).
        epoch: UTC datetime for the ECEF-to-ECI rotation; defaults to current UTC time.

    Returns:
        Dict with keys ``inclination_deg``, ``mean_motion_rev_per_day``,
        ``argument_of_perigee_deg``, ``mean_anomaly_deg``, and ``raan_deg``.
    """
    if epoch is None:
        epoch = datetime.now(timezone.utc)
    elements = geodetic_to_orbital_sgp4(
        lat_deg=lat_deg,
        lon_deg=lon_deg,
        alt_km=alt_km,
        inc_deg=incl_deg,
        epoch=epoch,
    )
    return {
        "inclination_deg": float(incl_deg),
        "mean_motion_rev_per_day": float(elements["mean_motion_rev_per_day"]),
        "argument_of_perigee_deg": float(np.degrees(elements["arg_perigee_rad"])),
        "mean_anomaly_deg": float(np.degrees(elements["mean_anomaly_rad"])),
        "raan_deg": float(np.degrees(elements["raan_rad"])),
    }


def geodetic_to_ecef(lat, lon, alt):
    """
    Convert geodetic coordinates (latitude, longitude, altitude) to ECEF (km).
    lat, lon in radians; alt in kilometers.
    :param lat: Latitude in radians.
    :param lon: Longitude in radians.
    :param alt: Altitude in kilometers.
    :return: ECEF coordinates as a numpy array [x, y, z] in kilometers.
    """
    # WGS-84 Earth constants
    a = 6378.137  # Equatorial radius (km)
    f = 1 / 298.257223563  # Flattening
    e_sq = f * (2 - f)  # Square of eccentricity

    sin_lat = np.sin(lat)
    N = a / np.sqrt(1 - e_sq * sin_lat**2)

    x = (N + alt) * np.cos(lat) * np.cos(lon)
    y = (N + alt) * np.cos(lat) * np.sin(lon)
    z = (N * (1 - e_sq) + alt) * sin_lat

    return np.array([x, y, z])


def gmst_from_jd(JD_utc):
    """
    Compute Greenwich Mean Sidereal Time (GMST) in radians for a given Julian Date (UTC).
    Formula from IAU 1982 model (approx. ±0.1 ms accuracy).
    """
    # Centuries since J2000.0:
    T = (JD_utc - 2451545.0) / 36525.0

    # GMST in degrees:
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (JD_utc - 2451545.0)
        + 0.000387933 * T**2
        - (T**3) / 38710000.0
    )
    # Reduce to [0, 360)
    gmst_deg = gmst_deg % 360.0
    # Convert to radians
    return np.deg2rad(gmst_deg)


def ecef_to_eci(r_ecef, gmst):
    """
    Rotate ECEF coordinates to ECI at a given GMST (radians).

    The transformation is a right-handed rotation about the z-axis by
    ``+GMST`` (``Rz(+gmst)``), because the ECI frame is fixed in inertial
    space while the ECEF frame rotates with the Earth.

    :param r_ecef: ECEF coordinates as a numpy array [x, y, z] in kilometers.
    :param gmst: Greenwich Mean Sidereal Time in radians.
    :return: ECI coordinates as a numpy array [x, y, z] in kilometers.
    """
    cos_g = np.cos(gmst)
    sin_g = np.sin(gmst)
    R = np.array([[cos_g, -sin_g, 0], [sin_g, cos_g, 0], [0, 0, 1]])
    return R.dot(r_ecef)


def geodetic_to_orbital_sgp4(lat_deg, lon_deg, alt_km, inc_deg, epoch):
    """
    Convert geodetic latitude (deg), longitude (deg), altitude (km),
    inclination (deg), and epoch (datetime UTC) to:
      - a Satrec object initialized via SGP4
      - and also return the computed classical elements:
        mean motion (rev/day), RAAN (rad), argument of perigee (rad),
        mean anomaly (rad).

    Raises:
        ValueError: If ``inc_deg`` is zero.  The RAAN solution divides by
            ``sin(inclination)``, so an equatorial (zero-inclination) orbit
            would produce NaN.
    """
    # 1) Convert inputs to radians/units
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    inc = np.radians(inc_deg)

    if float(inc_deg) == 0.0:
        raise ValueError(
            "inc_deg must be non-zero: the RAAN solution divides by "
            "sin(inclination) and is undefined for equatorial orbits"
        )

    # 2) Geodetic to ECEF
    r_ecef = geodetic_to_ecef(lat, lon, alt_km)

    # 3) Compute GMST & rotate to ECI
    jd = julian_date(epoch)
    gmst = gmst_from_jd(jd)
    r_eci = ecef_to_eci(r_ecef, gmst)
    x, y, z = r_eci
    r_norm = np.linalg.norm(r_eci)

    # 4) For circular orbit: a = r, e = 0
    mu = 398600.4418  # km^3/s^2
    a = r_norm
    n_rad_s = np.sqrt(mu / a**3)
    n_rev_per_day = (n_rad_s / (2 * np.pi)) * 86400.0

    # 5) Compute RAAN
    alpha = np.arctan2(y, x)
    rho = np.hypot(x, y)
    if rho < 1e-8:
        # Polar or near-polar, choose RAAN = 0 by convention
        Omega = 0.0
    else:
        S = -(z * (np.cos(inc) / np.sin(inc))) / rho  # S = -z * cot(i) / rho
        S = np.clip(S, -1.0, 1.0)
        delta = np.arcsin(S)
        Omega = (alpha + delta) % (2 * np.pi)

    # 6) Argument of perigee = 0 (circular orbit)
    omega = 0.0

    # 7) Compute mean anomaly from argument of latitude
    P = np.array([np.cos(Omega), np.sin(Omega), 0.0])
    Q = np.array(
        [-np.cos(inc) * np.sin(Omega), np.cos(inc) * np.cos(Omega), np.sin(inc)]
    )
    r_dot_P = np.dot(r_eci, P)
    r_dot_Q = np.dot(r_eci, Q)
    u = np.arctan2(r_dot_Q, r_dot_P) % (2 * np.pi)
    M = u  # for e = 0

    # 8) Prepare SGP4 epoch inputs

    return {
        "mean_motion_rev_per_day": n_rev_per_day,
        "raan_rad": Omega,
        "arg_perigee_rad": omega,
        "mean_anomaly_rad": M,
    }


def julian_date(dt):
    """
    Compute the Julian Date for a given datetime in UTC.
    """
    year = dt.year
    month = dt.month
    day = (
        dt.day
        + (dt.hour + dt.minute / 60 + dt.second / 3600 + dt.microsecond / 3.6e9) / 24.0
    )

    if month <= 2:
        year -= 1
        month += 12

    A = np.floor(year / 100)
    B = 2 - A + np.floor(A / 4)
    jd = (
        np.floor(365.25 * (year + 4716))
        + np.floor(30.6001 * (month + 1))
        + day
        + B
        - 1524.5
    )
    return jd


# Example usage:
if __name__ == "__main__":
    # Example usage:
    epoch = datetime(2025, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    result = geodetic_to_orbital_sgp4(
        lat_deg=10.0, lon_deg=45.0, alt_km=500.0, inc_deg=45.0, epoch=epoch
    )
    print(result["mean_motion_rev_per_day"])
    print(result["raan_rad"], result["arg_perigee_rad"], result["mean_anomaly_rad"])
