from __future__ import annotations

import math
from typing import Any, Optional, Tuple

import numpy as np

__all__ = [
    "coverage_ring_from_elevation",
    "coverage_percentage",
    "coverage_percentage_fast",
]


def coverage_ring_from_elevation(
    lat0_deg: float,
    lon0_deg: float,
    alt_km: float,
    elev_min_deg: float = 10.0,
    n_pts: int = 180,
    R_earth_km: float = 6_371.0,
) -> Tuple[np.ndarray, np.ndarray, bool] | Tuple[None, None, bool]:
    """
    Compute the visibility boundary ring for a satellite given a minimum
    elevation constraint.

    Returns a tuple (lon_deg, lat_deg, polar_flag) where arrays trace the
    boundary in degrees. If the geometry is impossible (e.g., too low altitude
    for the requested elevation), returns (None, None, False).

    polar_flag is True when the ring crosses a pole and requires special
    handling for dateline wrapping.
    """
    flag = False
    r = R_earth_km + float(alt_km)
    epsilon = math.radians(elev_min_deg)

    disc = r * r - (R_earth_km * math.cos(epsilon)) ** 2
    if disc < 0:
        # orbit too low ⇒ no feasible ring
        return None, None, False

    cospsi = (
        R_earth_km * math.cos(epsilon) ** 2 + math.sin(epsilon) * math.sqrt(disc)
    ) / r
    cospsi = max(-1.0, min(1.0, cospsi))
    psi = math.acos(cospsi)
    sinpsi, cospsi = math.sin(psi), math.cos(psi)

    bearings = np.linspace(0.0, 2 * math.pi, int(n_pts), endpoint=False)

    lat0 = math.radians(lat0_deg)
    lon0 = math.radians(lon0_deg)
    sin_lat0, cos_lat0 = math.sin(lat0), math.cos(lat0)

    lat = np.arcsin(sin_lat0 * cospsi + cos_lat0 * sinpsi * np.cos(bearings))
    lon = lon0 + np.arctan2(
        np.sin(bearings) * sinpsi * cos_lat0,
        cospsi - sin_lat0 * np.sin(lat),
    )

    # Detect large wrap (close to full range) that typically indicates a polar crossing
    diff = float(np.abs(np.max(lon) - np.min(lon)))
    if diff > 1.5 * np.pi:
        flag = True
        mean = float(np.mean(lat))
        north = mean > 0

        paired = list(zip(lon, lat))
        paired.sort(key=lambda x: x[0])
        lon, lat = zip(*paired)
        lon = np.array(lon)
        lat = np.array(lat)

        # Expand range for seamless wrapping when rasterizing
        lon = np.insert(lon, 0, -2 * np.pi)
        lon = np.append(lon, 2 * np.pi)
        lon = np.insert(lon, 0, -2 * np.pi)
        lon = np.append(lon, 2 * np.pi)

        if north:
            lat = np.insert(lat, 0, np.pi)
            lat = np.append(lat, np.pi)
            lat = np.insert(lat, 0, np.pi)
            lat = np.append(lat, np.pi)
        else:
            lat = np.insert(lat, 0, -np.pi)
            lat = np.append(lat, -np.pi)
            lat = np.insert(lat, 0, -np.pi)
            lat = np.append(lat, -np.pi)

    lat_deg = np.degrees(lat)
    lon_deg = np.degrees(lon)
    return lon_deg, lat_deg, flag


def coverage_percentage(
    sats: list[Any],
    elev_min_deg: float = 10.0,
    n_ring_pts: int = 360,
    grid_res_deg: float = 1.0,
    max_latitude_deg: float = 90.0,
) -> float:
    """
    Approximate percentage of Earth's surface covered by at least one satellite.

    Uses polygon membership tests on a sampling grid. More accurate but slower
    than the spherical approximation.
    """
    # Lazy import to avoid forcing matplotlib on consumers using fast methods only
    from matplotlib.path import Path  # type: ignore

    rings: list[Path] = []
    for sat in sats:
        lon_arr, lat_arr, _ = coverage_ring_from_elevation(
            lat0_deg=sat.position["latitude"],
            lon0_deg=sat.position["longitude"],
            alt_km=float(sat.position.get("altitude", 0.0)),
            elev_min_deg=elev_min_deg,
            n_pts=n_ring_pts,
            R_earth_km=6_371.0,
        )
        if lon_arr is None:
            continue
        rings.append(Path(np.vstack((lon_arr, lat_arr)).T))

        # Add wrapped copies when crossing the dateline
        if np.any(lon_arr < -180.0):
            rings.append(Path(np.vstack((lon_arr + 360.0, lat_arr)).T))
        if np.any(lon_arr > 180.0):
            rings.append(Path(np.vstack((lon_arr - 360.0, lat_arr)).T))

    if not rings:
        return 0.0

    lat_edges = np.arange(
        -max_latitude_deg, max_latitude_deg + grid_res_deg, grid_res_deg
    )
    lon_edges = np.arange(-180.0, 180.0 + grid_res_deg, grid_res_deg)
    lat_centers = lat_edges[:-1] + grid_res_deg / 2.0
    lon_centers = lon_edges[:-1] + grid_res_deg / 2.0
    lon_grid, lat_grid = np.meshgrid(lon_centers, lat_centers)

    pts = np.vstack((lon_grid.ravel(), lat_grid.ravel())).T
    cover_mask = np.zeros(pts.shape[0], dtype=bool)
    for path in rings:
        cover_mask |= path.contains_points(pts)

    lat_rads = np.radians(pts[:, 1])
    weights = np.cos(lat_rads)
    covered_weight = float(np.sum(weights[cover_mask]))
    total_weight = float(np.sum(weights))
    if total_weight <= 0:
        return 0.0
    return covered_weight / total_weight * 100.0


def coverage_percentage_fast(
    sats: list[Any],
    elev_min_deg: float = 10.0,
    grid_res_deg: float = 1.0,
    max_latitude_deg: float = 90.0,
    R_earth_km: float = 6_371.0,
) -> float:
    """
    Faster spherical approximation for the coverage percentage.

    A ground point (lat, lon) is covered by a satellite if the central angle
    between the point and the satellite subpoint is <= psi, where psi depends on
    altitude and the minimum elevation mask.
    """
    if not sats:
        return 0.0

    # Precompute psi (expressed via its cosine) for each satellite
    epsilon = math.radians(elev_min_deg)
    cos_e = math.cos(epsilon)
    sin_e = math.sin(epsilon)

    cospsi_list: list[float] = []
    sat_lat_list: list[float] = []
    sat_lon_list: list[float] = []

    for sat in sats:
        alt_km = float(sat.position.get("altitude", 0.0))
        r = R_earth_km + alt_km
        disc = r * r - (R_earth_km * cos_e) ** 2
        if disc < 0:
            continue
        cospsi = (R_earth_km * (cos_e**2) + sin_e * math.sqrt(disc)) / r
        cospsi = max(-1.0, min(1.0, cospsi))
        cospsi_list.append(cospsi)
        sat_lat_list.append(math.radians(sat.position["latitude"]))
        sat_lon_list.append(math.radians(sat.position["longitude"]))

    if not cospsi_list:
        return 0.0

    cospsi_arr = np.array(cospsi_list)
    sat_lat_arr = np.array(sat_lat_list)
    sat_lon_arr = np.array(sat_lon_list)

    # Sampling grid (centers of cells)
    lat_edges = np.arange(
        -max_latitude_deg, max_latitude_deg + grid_res_deg, grid_res_deg
    )
    lon_edges = np.arange(-180.0, 180.0 + grid_res_deg, grid_res_deg)
    lat_centers = np.radians(lat_edges[:-1] + grid_res_deg / 2.0)
    lon_centers = np.radians(lon_edges[:-1] + grid_res_deg / 2.0)
    lon_grid, lat_grid = np.meshgrid(lon_centers, lat_centers)

    sin_lat_grid = np.sin(lat_grid)
    cos_lat_grid = np.cos(lat_grid)

    covered = np.zeros(lon_grid.shape, dtype=bool)
    for s in range(cospsi_arr.shape[0]):
        sin_lat_sat = math.sin(sat_lat_arr[s])
        cos_lat_sat = math.cos(sat_lat_arr[s])
        dlon = lon_grid - sat_lon_arr[s]
        # Spherical law of cosines for central angle
        cos_c = sin_lat_grid * sin_lat_sat + cos_lat_grid * cos_lat_sat * np.cos(dlon)
        covered |= cos_c >= cospsi_arr[s]

    weights = np.cos(lat_grid)
    covered_weight = float(np.sum(weights[covered]))
    total_weight = float(np.sum(weights))
    if total_weight <= 0:
        return 0.0
    return covered_weight / total_weight * 100.0
