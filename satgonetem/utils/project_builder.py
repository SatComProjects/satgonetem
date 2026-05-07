"""Builder API for satcom topology projects.

This module is the primary entry point for constructing and running satcom
topology projects from Python.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any, Dict, List, Literal
import pathlib

from sat_com_builder.configuration_manager import BaseConfigurationManager
from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty
from sat_com_application.simulation_manager import SimulationManager
from sat_com_model.models import (
    create_satellite,
    create_ground_station,
    GroundStation as SatComGroundStation,
    Satellite as SatComSatellite,
    InterSatelliteLinkDirection,
)

from satgonetem.utils.satcom_fix import PyOrbitalModel


@dataclass
class GroundStationEntry:
    """A single ground station or user terminal entry.

    Args:
        index: Unique integer index for this entry.
        name: Human-readable name (e.g. city name).
        latitude: Latitude in decimal degrees.
        longitude: Longitude in decimal degrees.
        elevation_km: Elevation above sea level in kilometres.
    """

    index: int
    name: str
    latitude: float
    longitude: float
    elevation_km: float

    def to_csv_line(self) -> str:
        """Return the entry formatted as a single CSV line (no newline).

        Returns:
            Comma-separated string: index,name,latitude,longitude,elevation_km.
        """
        return f"{self.index},{self.name},{self.latitude},{self.longitude},{self.elevation_km}"


class GroundObjectFile:
    """A named collection of ground object entries that writes to disk on demand.

    Entries are written in CSV format (no header) compatible with the
    sat_com_topology ground station file format:
        index,name,latitude,longitude,elevation_km

    Args:
        identifier: Human-readable name used as the filename stem and as the
            ground object identifier in the project configuration.
        entries: Ordered list of GroundStationEntry objects to write.
    """

    def __init__(self, identifier: str, entries: List[GroundStationEntry]) -> None:
        self.identifier = identifier
        self.entries = entries

    def write(self, base_dir: str = "/tmp") -> str:
        """Write all entries to a CSV file under base_dir and return its path.

        The filename is derived from identifier by lowercasing and replacing
        spaces with underscores (e.g. "Ground Stations" -> "ground_stations.txt").

        Args:
            base_dir: Directory in which to create the file. Defaults to /tmp.

        Returns:
            Absolute path of the written file as a string.

        Raises:
            OSError: If the file cannot be written.
        """
        safe_name = self.identifier.replace(" ", "_").lower()
        file_path = pathlib.Path(base_dir) / f"{safe_name}.txt"
        with open(file_path, "w") as f:
            for entry in self.entries:
                f.write(entry.to_csv_line() + "\n")
        return str(file_path)

    @classmethod
    def from_csv(cls, identifier: str, csv_path: str) -> "GroundObjectFile":
        """Create a GroundObjectFile from a CSV file.

        The CSV file should have no header and follow the format:
            index,name,latitude,longitude,elevation_km
        Args:
            identifier: Human-readable name for this ground object file.
            csv_path: Path to the input CSV file.
        Returns:
            A GroundObjectFile instance with entries parsed from the CSV.
        Raises:
            OSError: If the file cannot be read.
            ValueError: If any line in the CSV is malformed.
        """
        entries = []
        with open(csv_path, "r") as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 5:
                    raise ValueError(f"Malformed line in CSV: {line}")
                index, name, lat, lon, elev = parts
                entry = GroundStationEntry(
                    index=int(index),
                    name=name,
                    latitude=float(lat),
                    longitude=float(lon),
                    elevation_km=float(elev),
                )
                entries.append(entry)
        return cls(identifier=identifier, entries=entries)


class GroundObject:
    """Builder that couples a GroundObjectFile with its type and connectivity settings.

    This class is the user-facing builder equivalent of the sat_com_models
    GroundObject Pydantic model. It defers file writing until to_dict() is called.

    Args:
        ground_object_file: The GroundObjectFile containing station entries.
        object_type: Either "ground_station" or "user_terminal".
        connectivity_properties: Connectivity settings for this ground object group.
    """

    def __init__(
        self,
        ground_object_file: GroundObjectFile,
        object_type: Literal["ground_station", "user_terminal"],
        connectivity_properties: GroundConnectivityProperty,
    ) -> None:
        self.ground_object_file = ground_object_file
        self.object_type = object_type
        self.connectivity_properties = connectivity_properties

    def to_dict(self, base_dir: str = "/tmp") -> Dict[str, Any]:
        """Write the ground object file and return the configuration dict entry.

        Args:
            base_dir: Directory in which to write the ground object CSV file.

        Returns:
            Dict matching the ground_objects_properties entry schema expected
            by create_satcom_project().

        Raises:
            OSError: If the underlying file cannot be written.
        """
        file_path = self.ground_object_file.write(base_dir)
        return {
            "identifier": self.ground_object_file.identifier,
            "data_file": file_path,
            "type": self.object_type,
            "connectivity_properties": self.connectivity_properties.model_dump(),
        }


class DictConfigurationManager(BaseConfigurationManager):
    """Wraps BaseConfigurationManager to accept a plain dictionary.

    Args:
        dictionary: Simulation property dict matching the SimulationProperty schema.
    """

    def __init__(self, dictionary: dict) -> None:
        simulation_property = SimulationProperty(**dictionary)
        super().__init__(simulation_property)


def create_and_load_simulation(
    dict_configuration: Dict[str, Any], project_name: str
) -> SimulationManager:
    """Create and load a simulation from a configuration dictionary.

    Args:
        dict_configuration: Sat-com configuration dict matching the
            SimulationProperty schema.
        project_name: Name assigned to the simulation project.

    Returns:
        Initialised SimulationManager instance.
    """
    logging.info("Creating and loading simulation.")
    manager = DictConfigurationManager(dictionary=dict_configuration)
    simulation_manager = manager.load_simulation()
    return simulation_manager


def create_custom_satellite(
    custom_sat: dict,
    simulation_manager: SimulationManager,
    object_id: int = 0,
    domain: str = "public",
) -> int:
    """Create a TLE-based satellite and add it to the simulation manager.

    Args:
        custom_sat: Dict with keys name, tle_line1, tle_line2.
        simulation_manager: Active SimulationManager to receive the satellite.
        object_id: Business identifier for the satellite. Defaults to 0.
        domain: Domain identifier. Defaults to "public".

    Returns:
        The topology_uniq_id of the newly added satellite.
    """
    before_ids = {sat.topology_uniq_id for sat in simulation_manager.get_satellites()}
    satellite_model = PyOrbitalModel(
        tle={
            "satellite_name": custom_sat["name"],
            "line1": custom_sat["tle_line1"],
            "line2": custom_sat["tle_line2"],
        },
        time_manager=simulation_manager.time_manager,
    )
    sat_name: str = satellite_model.satellite_name
    new_sat = create_satellite(sat_name, object_id, domain)
    new_sat.set_movement_model(satellite_model)
    simulation_manager.add_satellite(new_sat)
    simulation_manager.update_ground_station_links()
    after_ids = {sat.topology_uniq_id for sat in simulation_manager.get_satellites()}
    new_ids = after_ids - before_ids
    if len(new_ids) != 1:
        raise RuntimeError(
            f"Expected exactly one new satellite id, found {len(new_ids)}: {new_ids}"
        )
    new_id = new_ids.pop()
    logging.info(f"Custom satellite {new_id} added")
    return int(new_id)


def add_custom_satellites(
    simulation_manager: SimulationManager, custom_satellites: List[Dict[str, Any]]
) -> None:
    """Add all custom satellites to the simulation manager.

    Args:
        simulation_manager: Active SimulationManager instance.
        custom_satellites: List of dicts, each with name, tle_line1, tle_line2.
    """
    if not custom_satellites:
        return
    for custom_sat in custom_satellites:
        create_custom_satellite(custom_sat, simulation_manager)


def create_custom_ground_station(
    name: str,
    latitude: float,
    longitude: float,
    elevation_km: float,
    object_id: int = 0,
    domain: str = "public",
) -> SatComGroundStation:
    """Create a fixed-position ground station.

    Args:
        name: Human-readable label (e.g. city name).
        latitude: Latitude in decimal degrees.
        longitude: Longitude in decimal degrees.
        elevation_km: Elevation above sea level in kilometres.
        object_id: Business identifier for the ground station. Defaults to 0.
        domain: Domain identifier. Defaults to "public".

    Returns:
        A sat_com_model GroundStation instance with position set.
    """
    gs = create_ground_station(object_id=object_id, domain=domain)
    gs.label = name
    gs.set_position(
        latitude=latitude, longitude=longitude, altitude=elevation_km * 1000
    )
    return gs


def add_custom_ground_station(
    ground_station: SatComGroundStation, simulation_manager: SimulationManager
) -> None:
    """Add a ground station to an existing simulation manager.

    The ground station is appended to the manager but automatic ground-station
    links are **not** refreshed.  If you want the simulation manager to connect
    the station to visible satellites automatically, set a
    `ground_object_domain` on the ground station and call
    `simulation_manager.update_ground_station_links()` yourself after all
    stations have been added.

    Args:
        ground_station: GroundStation instance created by create_custom_ground_station.
        simulation_manager: Active SimulationManager to receive the ground station.
    """
    simulation_manager.add_ground_station(ground_station)
    logging.info(f"Custom ground station {ground_station.topology_uniq_id} added")




def _get_orbital_elements(
    sat: SatComSatellite, time_manager
) -> tuple[float, float, float] | None:
    """Extract RAAN, orbital phase and altitude from a satellite's SGP4 model.

    RAAN is taken directly from the TLE epoch elements.  Orbital phase is the
    argument of latitude (degrees) computed from the *propagated* TEME position,
    so it reflects the satellite's actual geometric location along its orbital
    track.  Altitude is read from the movement model's geodetic position.

    Args:
        sat: A Satellite with a PyOrbitalModel movement model.
        time_manager: The simulation's TimeManager (provides current UTC).

    Returns:
        Tuple of (raan_degrees, orbital_phase_degrees, altitude_km)
        or None if the satellite has no usable orbital model.
    """
    import math

    mm = sat.get_movement_model()
    orb = getattr(mm, "orbital_object", None)
    if orb is None:
        return None

    raan = math.degrees(orb.nodeo)
    inc = math.radians(math.degrees(orb.inclo))
    raan_rad = math.radians(raan)

    # Use the movement model's propagated position (NOT manual sgp4)
    r_teme, _v_teme = mm.get_position_earth_general_inertial()
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
    alt_m = mm.get_longitude_latitude_altitude().altitude
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
                r_a = sat_a.get_movement_model().get_position_earth_general_inertial()[0]
                r_b = sat_b.get_movement_model().get_position_earth_general_inertial()[0]
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


def create_test_project(
    simulation_name: str = "TestConstellation",
    start_date: str = "",
    end_date: str = "",
    ground_stations_file: str = "",
) -> SimulationProperty:
    """Build a minimal SimulationProperty suitable for tests and quick experiments.

    Uses a small Iridium-like Walker Star constellation (7 planes x 11 sats,
    86.4 deg inclination) and a default set of five European ground stations
    when none are supplied.

    Args:
        simulation_name: Name assigned to the simulation. Defaults to
            "TestConstellation".
        start_date: Simulation start datetime string in format
            "DD/MM/YYYY HH:MM:SS". If empty, defaults to the current time
            rounded down to the minute.
        end_date: Simulation end datetime string in the same format. If empty,
            defaults to 10 minutes after *start_date*.
        ground_stations_file: Optional path to a CSV file containing ground
            station entries. If not provided, a default set of five European
            stations is used.

    Returns:
        A fully configured SimulationProperty.

    Raises:
        OSError: If the ground object CSV file cannot be written.
    """
    now = datetime.now(timezone.utc)  # .replace(second=0, microsecond=0)
    if not start_date:
        start_date = now.strftime("%d/%m/%Y %H:%M:%S")
    if not end_date:
        end_date = (now + timedelta(minutes=10)).strftime("%d/%m/%Y %H:%M:%S")
    ground_files_dir = "/tmp/"
    if len(ground_stations_file) == 0:
        ground_stations = [
            GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
            GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
            GroundStationEntry(2, "Paris", 48.856, 2.352, 0.035),
            GroundStationEntry(3, "Rome", 41.902, 12.496, 0.021),
            GroundStationEntry(4, "Madrid", 40.416, -3.703, 0.667),
        ]
        gs_file = GroundObjectFile("Ground Stations", ground_stations)
        data_file = gs_file.write(ground_files_dir)

    elif len(ground_stations_file) > 0:

        ground_stations = GroundObjectFile.from_csv(
            identifier="Ground Stations", csv_path=ground_stations_file
        ).entries

        gs_file = GroundObjectFile("Ground Stations", ground_stations)
        data_file = gs_file.write(ground_files_dir)

    ground_object_property = GroundObjectProperty(
        identifier=gs_file.identifier,
        data_file=data_file,
        type="ground_station",
        connectivity_properties=GroundConnectivityProperty(
            ground_to_space_connections_strategy="best-angle-until-disconnection",
            elevation_above_horizon=10,
            maximum_satellite_range_distance=1500.0,
            shell_white_lists=["LEO"],
            maximum_connected_satellites=1,
        ),
    )

    shell = WalkerShellProperty(
        type="star",
        constellation_property=WalkerConstellationProperty(
            identifier="LEO",
            amount_of_orbit_plane=7,
            amount_of_satellite_per_orbit_plane=11,
            inclination=86.4,
            mean_revolution_per_day=14.35,
            phase_difference_between_satellites=True,
        ),
        orbital_connectivity_property=OrbitalConnectivityProperty(
            adjacent_inter_satellite_shifting=0,
            maximum_inter_satellite_count=4,
            maximum_inter_satellite_range_distance=1500.0,
            maximum_ground_station_range=1200.0,
            maximum_user_terminal_range=1000.0,
            maximum_connected_ground_object=10000,
            maximum_connected_user_terminal=500,
            maximum_connected_ground_station=10,
        ),
        ground_object_white_list=["Ground Stations"],
    )

    return SimulationProperty(
        simulation_name=simulation_name,
        start_date=start_date,
        end_date=end_date,
        ground_objects_properties=[ground_object_property],
        walker_shells=[shell],
    )


def create_empty_project(
    simulation_name: str = "TestConstellation",
    start_date: str = "",
    end_date: str = "",
) -> SimulationProperty:
    """Build a minimal SimulationProperty with no satellites or ground stations.

    Args:
        simulation_name: Name assigned to the simulation. Defaults to
            "TestConstellation".
        start_date: Simulation start datetime string in format
            "DD/MM/YYYY HH:MM:SS". If empty, defaults to the current time
            rounded down to the minute.
        end_date: Simulation end datetime string in the same format. If empty,
            defaults to 10 minutes after *start_date*.

    Returns:
        A fully configured SimulationProperty.

    Raises:
        OSError: If the ground object CSV file cannot be written.
    """
    now = datetime.now(timezone.utc)  # .replace(second=0, microsecond=0)
    if not start_date:
        start_date = now.strftime("%d/%m/%Y %H:%M:%S")
    if not end_date:
        end_date = (now + timedelta(minutes=10)).strftime("%d/%m/%Y %H:%M:%S")

    return SimulationProperty(
        simulation_name=simulation_name,
        start_date=start_date,
        end_date=end_date,
        ground_objects_properties=[],
        walker_shells=[],
    )
