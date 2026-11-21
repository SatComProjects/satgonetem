"""Mesh/constellation link construction helpers."""

from __future__ import annotations

import logging
import math

from sat_com_application.simulation_manager import SimulationManager
from sat_com_model.models import InterSatelliteLinkDirection
from sat_com_model.models import Satellite as SatComSatellite


def _get_orbital_elements(
    sat: SatComSatellite, time_manager
) -> tuple[float, float, float] | None:
    """Extract RAAN, orbital phase and altitude from a satellite's orbital model.

    For SGP4 models RAAN and inclination are taken directly from the TLE epoch
    elements (``nodeo``/``inclo``).  Models such as pyorbital's ``Orbital`` do
    not expose those attributes, so the orbital-plane orientation is derived
    from the propagated TEME state instead: ``h = r x v``,
    ``inclination = acos(h_z / |h|)`` and ``RAAN = atan2(h_x, -h_y)``.

    Orbital phase is the argument of latitude (degrees) computed from the
    *propagated* TEME position, so it reflects the satellite's actual geometric
    location along its orbital track.  Altitude is read from the movement
    model's geodetic position.

    Args:
        sat: A Satellite with a movement model that propagates a TEME state.
        time_manager: The simulation's TimeManager (provides current UTC).

    Returns:
        Tuple of (raan_degrees, orbital_phase_degrees, altitude_km)
        or None if the satellite has no usable orbital model.
    """
    import math

    mm = sat.get_movement_model()
    if mm is None:
        return None

    orb = getattr(mm, "orbital_object", None)
    if orb is not None and hasattr(orb, "nodeo") and hasattr(orb, "inclo"):
        # Satrec stores both angles in radians; keep RAAN in [0, 360).
        raan = math.degrees(orb.nodeo) % 360.0
        inc = float(orb.inclo)
        r_teme = mm.get_position_earth_general_inertial()
    else:
        # pyorbital's Orbital has no TLE element attributes.  Derive the
        # orbital-plane orientation from the propagated state vector.
        get_position = getattr(mm, "get_position_earth_general_inertial", None)
        if get_position is None:
            return None

        r_teme = get_position()
        get_velocity = getattr(mm, "get_velocity_earth_general_inertial", None)
        if get_velocity is None:
            return None
        v_teme = get_velocity()
        x, y, z = r_teme
        vx, vy, vz = v_teme
        h_x = y * vz - z * vy
        h_y = z * vx - x * vz
        h_z = x * vy - y * vx
        h_norm = math.sqrt(h_x * h_x + h_y * h_y + h_z * h_z)
        if h_norm == 0.0:
            return None

        inc = math.acos(max(-1.0, min(1.0, h_z / h_norm)))
        raan = math.degrees(math.atan2(h_x, -h_y)) % 360.0

    raan_rad = math.radians(raan)

    # Use the movement model's propagated position (NOT manual sgp4)
    x, y, z = r_teme

    # Orbital-plane unit vectors
    x_hat = (math.cos(raan_rad), math.sin(raan_rad), 0.0)
    y_hat = (
        -math.cos(inc) * math.sin(raan_rad),
        math.cos(inc) * math.cos(raan_rad),
        math.sin(inc),
    )

    # Project position onto orbital plane and compute argument of latitude
    rx = x * x_hat[0] + y * x_hat[1] + z * x_hat[2]
    ry = x * y_hat[0] + y * y_hat[1] + z * y_hat[2]
    phase = math.degrees(math.atan2(ry, rx)) % 360.0

    # Altitude from geodetic position (metres → km)
    get_geodetic = getattr(mm, "get_longitude_latitude_altitude", None)
    if get_geodetic is None:
        return None
    alt_m = get_geodetic().altitude
    altitude = alt_m / 1000.0

    return raan, phase, altitude


def _group_satellites_by_orbital_plane(
    satellites,
    time_manager,
    raan_threshold: float = 5.0,
    min_plane_size: int = 10,
    max_altitude_difference: float = 50.0,
) -> list[list[tuple[SatComSatellite, float]]]:
    """Group satellites into orbital planes by RAAN and altitude.

    Satellites are first grouped by RAAN.  Within each RAAN cluster the
    group is further split by altitude: if the altitude gap between two
    consecutive satellites (when sorted by altitude) exceeds
    *max_altitude_difference*, a new sub-plane is started.  This prevents
    outliers (e.g. deorbiting satellites) from being meshed with the main
    constellation.

    Args:
        satellites: List of Satellite objects.
        time_manager: The simulation's TimeManager.
        raan_threshold: Maximum RAAN difference (degrees) to belong to the same plane.
        min_plane_size: Minimum number of satellites to consider a valid plane.
        max_altitude_difference: Maximum altitude difference (km) between
            satellites in the same plane.  Satellites whose altitude differs
            by more than this value from the rest of their RAAN cluster are
            placed in separate sub-planes.

    Returns:
        List of orbital planes, where each plane is a list of
        (satellite, current_mean_anomaly) tuples.
    """
    sat_data = []
    for sat in satellites:
        elements = _get_orbital_elements(sat, time_manager)
        if elements is None:
            continue
        raan, current_ma, altitude = elements
        sat_data.append((sat, raan, current_ma, altitude))

    if not sat_data:
        return []

    # Sort by RAAN so we can cluster consecutive satellites
    sat_data.sort(key=lambda x: x[1])

    # Cluster by RAAN, handling the 0°/360° wrap-around by looking for the
    # largest gap and breaking the circle there.
    n = len(sat_data)
    if n == 0:
        return []

    # Find largest RAAN gap
    max_gap = -1.0
    break_idx = 0
    for i in range(n):
        next_i = (i + 1) % n
        diff = (sat_data[next_i][1] - sat_data[i][1]) % 360.0
        if diff > max_gap:
            max_gap = diff
            break_idx = i

    # Rotate so the largest gap is at the end
    rotated = sat_data[break_idx + 1 :] + sat_data[: break_idx + 1]

    # Now cluster by splitting whenever the RAAN gap exceeds the threshold
    raan_planes = []
    current_plane = [rotated[0]]
    for i in range(1, len(rotated)):
        diff = rotated[i][1] - rotated[i - 1][1]
        if diff < 0:
            diff += 360.0
        if diff < raan_threshold:
            current_plane.append(rotated[i])
        else:
            raan_planes.append(current_plane)
            current_plane = [rotated[i]]
    raan_planes.append(current_plane)

    # Within each RAAN plane, further split by altitude
    planes = []
    for raan_plane in raan_planes:
        # Sort by altitude
        raan_plane.sort(key=lambda x: x[3])
        current_sub = [raan_plane[0]]
        for i in range(1, len(raan_plane)):
            alt_diff = raan_plane[i][3] - raan_plane[i - 1][3]
            if alt_diff < max_altitude_difference:
                current_sub.append(raan_plane[i])
            else:
                planes.append(current_sub)
                current_sub = [raan_plane[i]]
        planes.append(current_sub)

    # Keep average RAAN for sorting, then strip RAAN and altitude
    planes_with_raan = []
    for plane in planes:
        avg_raan = sum(s[1] for s in plane) / len(plane)
        stripped = [(s[0], s[2]) for s in plane]
        planes_with_raan.append((avg_raan, stripped))

    # Sort planes by average RAAN so adjacent planes are truly adjacent in space
    planes_with_raan.sort(key=lambda x: x[0])
    return [p[1] for p in planes_with_raan]


def add_mesh_links(
    simulation_manager: SimulationManager,
    add_orbital: bool = True,
    add_adjacent: bool = True,
    add_ground: bool = True,
    max_altitude_difference: float = 50.0,
    min_plane_size: int = 10,
) -> None:
    """Create a mesh network between satellites and connect ground stations.

    The algorithm follows four deterministic steps:

    1. **Group by plane**: satellites are grouped by RAAN into orbital planes.
       Satellites whose altitude differs by more than *max_altitude_difference*
       from the rest of their RAAN cluster are placed in separate sub-planes.
    2. **Intra-plane links**: within each plane satellites are sorted by mean
       anomaly and connected sequentially (1→2, 2→3, …, last→0).
    3. **Inter-plane links**: for each pair of consecutive planes, every
       satellite in the first plane is connected to the *closest unmatched*
       satellite in the second plane (by angular mean-anomaly distance).
       The last plane does **not** wrap around to the first plane.
    4. **Ground links**: each ground station is connected to the closest
       satellite.

    All four steps can be toggled independently.

    Args:
        simulation_manager: Active SimulationManager instance.
        add_orbital: If True, create intra-plane (ORBITAL) links.
        add_adjacent: If True, create inter-plane (ADJACENT) links.
        add_ground: If True, connect ground stations to the closest satellite.
        max_altitude_difference: Maximum altitude difference (km) between
            satellites in the same plane.  Defaults to 50 km.
        min_plane_size: Minimum number of satellites a plane must have to
            participate in inter-plane (ADJACENT) links.  Singletons and
            small fragments are skipped.  Defaults to 10.
    """
    satellites = list(simulation_manager.get_satellites())
    ground_stations = list(simulation_manager.get_ground_stations())

    if len(satellites) < 2:
        logging.warning("Not enough satellites to create mesh links")
        return

    planes = _group_satellites_by_orbital_plane(
        satellites,
        simulation_manager.time_manager,
        max_altitude_difference=max_altitude_difference,
        min_plane_size=min_plane_size,
    )
    num_planes = len(planes)

    if num_planes == 0:
        logging.warning("Could not group satellites into orbital planes")
        return

    created_isls = 0
    created_gsls = 0

    # Step 2: intra-plane (orbital) links
    if add_orbital:
        for plane in planes:
            sorted_plane = sorted(plane, key=lambda x: x[1])
            n = len(sorted_plane)
            if n < 2:
                continue
            for i in range(n):
                sat_a = sorted_plane[i][0]
                sat_b = sorted_plane[(i + 1) % n][0]
                link = (
                    simulation_manager.create_and_add_inter_satellite_link_connection(
                        sat_a, sat_b
                    )
                )
                link.inter_satellite_direction = InterSatelliteLinkDirection.ORBITAL
                created_isls += 1

    # Step 3: inter-plane (adjacent) links
    if add_adjacent and num_planes >= 2:
        for i in range(num_planes - 1):
            plane_a = sorted(planes[i], key=lambda x: x[1])
            plane_b = sorted(planes[i + 1], key=lambda x: x[1])

            # Skip if either plane is too small (outliers / fragments)
            if len(plane_a) < min_plane_size or len(plane_b) < min_plane_size:
                continue

            for idx in range(min(len(plane_a), len(plane_b))):
                sat_a, _phase_a = plane_a[idx]
                sat_b, _phase_b = plane_b[idx]

                # Skip if the 3D distance is excessive (e.g. planes are far
                # apart in RAAN because intermediate planes are missing).
                r_a = sat_a.get_movement_model().get_position_earth_general_inertial()
                r_b = sat_b.get_movement_model().get_position_earth_general_inertial()
                dist = math.sqrt(
                    (r_a[0] - r_b[0]) ** 2
                    + (r_a[1] - r_b[1]) ** 2
                    + (r_a[2] - r_b[2]) ** 2
                )
                if dist > 4000.0:
                    continue

                link = simulation_manager.create_and_add_inter_satellite_link_connection(
                    sat_a, sat_b
                )
                link.inter_satellite_direction = InterSatelliteLinkDirection.ADJACENT
                created_isls += 1

    # Step 4: ground-station links
    if add_ground:
        for gs in ground_stations:
            try:
                closest_sat = (
                    simulation_manager.get_closest_satellite_to_a_ground_station(gs)
                )
                simulation_manager.create_and_add_ground_station_link_connection(
                    closest_sat, gs
                )
                created_gsls += 1
            except Exception as exc:
                logging.warning(f"Failed to connect ground station {gs.label}: {exc}")

    logging.info(f"Mesh links created: {created_isls} ISLs, {created_gsls} GSLs")
    print(f"Created {created_isls} ISLs, {created_gsls} GSLs")
