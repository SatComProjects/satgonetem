"""Builder API for satcom topology projects.

This module is the primary entry point for constructing and running satcom
topology projects from Python.
"""

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from typing import Any, Callable, Dict, List, Literal, Optional
import numpy as np
import os
import pathlib
import shutil
import time

from sat_com_builder.configuration_manager import BaseConfigurationManager
from sat_com_adapter.adapters import NetworkXAdapter
from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty
from sat_com_application.simulation_manager import SimulationManager
from sat_com_model.models import create_satellite
from sgp4.api import Satrec, WGS72, jday

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


class SatcomProject:
    """Top-level builder for a complete satcom topology project configuration.

    Composes GroundObject and WalkerShell instances into the configuration
    dict expected by create_satcom_project().

    Args:
        simulation_name: Name of the simulation (used as the project name).
        start_date: Simulation start datetime string (e.g. "01/01/2024 00:00:00").
        end_date: Simulation end datetime string (e.g. "01/01/2024 00:01:00").
        walker_shells: List of WalkerShell instances defining the constellation.
        ground_objects: List of GroundObject builder instances.
        movement_model: Orbital propagation model. Defaults to "pyorbital".
        distance_model: Distance calculation model. Defaults to "sklearn".
        disable_ground_station_link_preload: Disable link preload optimisation.
        static_ground_station_link_mode: Use static link mode for ground stations.
    """

    def __init__(
        self,
        simulation_name: str,
        start_date: str,
        end_date: str,
        walker_shells: List[WalkerShellProperty],
        ground_objects: List[GroundObject],
        movement_model: str = "pyorbital",
        distance_model: str = "sklearn",
        disable_ground_station_link_preload: bool = False,
        static_ground_station_link_mode: bool = False,
    ) -> None:
        self.simulation_name = simulation_name
        self.start_date = start_date
        self.end_date = end_date
        self.walker_shells = walker_shells
        self.ground_objects = ground_objects
        self.movement_model = movement_model
        self.distance_model = distance_model
        self.disable_ground_station_link_preload = disable_ground_station_link_preload
        self.static_ground_station_link_mode = static_ground_station_link_mode

    def to_sat_com_config_dict(self, ground_files_dir: str = "/tmp") -> Dict[str, Any]:
        """Write ground object files and return the full sat_com configuration dict.

        The returned dict can be passed directly as the sat_com_configuration
        argument of create_satcom_project().

        Args:
            ground_files_dir: Directory in which ground object CSV files are
                written. Defaults to /tmp.

        Returns:
            Dict matching the sat_com_config.yaml schema.

        Raises:
            OSError: If any ground object file cannot be written.
        """
        return {
            "simulation_name": self.simulation_name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "movement_model": self.movement_model,
            "distance_model": self.distance_model,
            "disable_ground_station_link_preload": self.disable_ground_station_link_preload,
            "static_ground_station_link_mode": self.static_ground_station_link_mode,
            "ground_objects_properties": [
                go.to_dict(ground_files_dir) for go in self.ground_objects
            ],
            "walker_shells": [shell.model_dump() for shell in self.walker_shells],
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


def _create_custom_satellite(
    custom_sat: dict, simulation_manager: SimulationManager
) -> None:
    """Create a TLE-based satellite and add it to the simulation manager.

    Args:
        custom_sat: Dict with keys name, tle_line1, tle_line2.
        simulation_manager: Active SimulationManager to receive the satellite.
    """
    satellite_model = PyOrbitalModel(
        tle={
            "satellite_name": custom_sat["name"],
            "line1": custom_sat["tle_line1"],
            "line2": custom_sat["tle_line2"],
        },
        time_manager=simulation_manager.time_manager,
    )
    sat_name: str = satellite_model.satellite_name
    new_sat = create_satellite(sat_name, 0)
    new_sat.set_movement_model(satellite_model)
    simulation_manager.add_satellite(new_sat)
    simulation_manager.update_ground_station_links()
    logging.info(f"Custom satellite {new_sat.topology_uniq_id} added")


def _add_custom_satellites(
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
        _create_custom_satellite(custom_sat, simulation_manager)


def create_test_project(
    simulation_name: str = "TestConstellation",
    start_date: str = "01/01/2024 00:00:00",
    end_date: str = "01/01/2024 00:01:00",
    ground_stations: Optional[List[GroundStationEntry]] = None,
    ground_files_dir: str = "/tmp",
) -> SimulationProperty:
    """Build a minimal SimulationProperty suitable for tests and quick experiments.

    Uses a small Iridium-like Walker Star constellation (7 planes x 11 sats,
    86.4 deg inclination) and a default set of five European ground stations
    when none are supplied.

    Args:
        simulation_name: Name assigned to the simulation. Defaults to
            "TestConstellation".
        start_date: Simulation start datetime string. Defaults to
            "01/01/2024 00:00:00".
        end_date: Simulation end datetime string. Defaults to
            "01/01/2024 00:01:00".
        ground_stations: Optional list of GroundStationEntry objects to use as
            the ground station group. When omitted, a small representative set
            is used.
        ground_files_dir: Directory where ground object CSV files are written.
            Defaults to "/tmp".

    Returns:
        A fully configured SimulationProperty.

    Raises:
        OSError: If the ground object CSV file cannot be written.
    """
    if ground_stations is None:
        ground_stations = [
            GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
            GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
            GroundStationEntry(2, "Paris", 48.856, 2.352, 0.035),
            GroundStationEntry(3, "Rome", 41.902, 12.496, 0.021),
            GroundStationEntry(4, "Madrid", 40.416, -3.703, 0.667),
        ]

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
            maximum_connected_satellites=3,
        ),
    )

    shell = WalkerShellProperty(
        type="delta",
        constellation_property=WalkerConstellationProperty(
            identifier="LEO",
            amount_of_orbit_plane=20,
            amount_of_satellite_per_orbit_plane=20,
            inclination=70,
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
