"""Sample and empty SimulationProperty fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty

from satgonetem.utils.project_ground_objects import GroundObjectFile, GroundStationEntry

_DATE_FORMAT = "%d/%m/%Y %H:%M:%S"


def _resolve_simulation_dates(start_date: str, end_date: str) -> tuple[str, str]:
    """Resolve optional simulation dates.

    An omitted end date is always ten minutes after the effective start date,
    not ten minutes after the wall clock.

    Args:
        start_date: Start datetime string, or empty for now.
        end_date: End datetime string, or empty for start + 10 minutes.

    Returns:
        Tuple of ``(start_date, end_date)`` in the documented format.
    """
    if start_date:
        start_dt = datetime.strptime(start_date, _DATE_FORMAT)
    else:
        start_dt = datetime.now(timezone.utc)
        start_date = start_dt.strftime(_DATE_FORMAT)

    if not end_date:
        end_date = (start_dt + timedelta(minutes=10)).strftime(_DATE_FORMAT)

    return start_date, end_date


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
    start_date, end_date = _resolve_simulation_dates(start_date, end_date)
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
    start_date, end_date = _resolve_simulation_dates(start_date, end_date)

    return SimulationProperty(
        simulation_name=simulation_name,
        start_date=start_date,
        end_date=end_date,
        ground_objects_properties=[],
        walker_shells=[],
    )
