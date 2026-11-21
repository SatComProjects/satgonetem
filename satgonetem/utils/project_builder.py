"""Builder API for satcom topology projects.

This module is the primary entry point for constructing and running satcom
topology projects from Python.
"""

import logging
from typing import Any, Dict, List

from sat_com_application.simulation_manager import SimulationManager
from sat_com_builder.configuration_manager import BaseConfigurationManager
from sat_com_builder.models import (
    SimulationProperty,
)
from sat_com_model.exceptions import GroundObjectDomainNotFoundError
from sat_com_model.models import (
    GroundObject as SatComGroundObject,
)
from sat_com_model.models import (
    GroundObjectDomain,
    create_ground_station,
)
from sat_com_model.models import (
    GroundStation as SatComGroundStation,
)
from sat_com_model.models import (
    Satellite as SatComSatellite,
)

from satgonetem.utils.project_fixtures import (
    create_empty_project as create_empty_project,
)
from satgonetem.utils.project_fixtures import (
    create_test_project as create_test_project,
)
from satgonetem.utils.project_ground_objects import (
    GroundObject as GroundObject,
)
from satgonetem.utils.project_ground_objects import (
    GroundObjectFile as GroundObjectFile,
)
from satgonetem.utils.project_ground_objects import (
    GroundStationEntry as GroundStationEntry,
)
from satgonetem.utils.project_mesh import (
    _get_orbital_elements as _get_orbital_elements,
)
from satgonetem.utils.project_mesh import add_mesh_links as add_mesh_links
from satgonetem.utils.satcom_fix import PyOrbitalModel


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
    # Built directly rather than with sat_com's create_satellite: sat_com_topology 5 inserted a required `shell`
    # parameter before `domain`. A TLE-only satellite belongs to no shell (shell stays None in 5, absent in 4).
    new_sat = SatComSatellite(satellite_name=sat_name, object_id=object_id, domain=domain)
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


def _attach_ground_object_domain(
    ground_object: SatComGroundObject, simulation_manager: SimulationManager
) -> None:
    """Attach a compatible ground-object domain to a custom ground object.

    Custom objects created by this module do not belong to a domain yet.  The
    simulation manager requires one for link updates and strategy resolution.
    Prefer an existing domain with the requested identifier, then any existing
    ground-station domain, and create a default one only if the simulation has
    no domains at all.

    Args:
        ground_object: Ground object to update in place.
        simulation_manager: Manager that owns the available domains.
    """
    if ground_object.ground_object_domain is not None:
        return

    domain = None
    try:
        domain = simulation_manager.get_ground_object_domain(ground_object.domain)
    except GroundObjectDomainNotFoundError:
        pass

    # The ownership ``domain`` string may resolve to a generic domain with no
    # connectivity strategy; a ground station needs a ground_station domain.
    if domain is None or getattr(domain, "type", None) != "ground_station":
        domain = next(
            (
                candidate
                for candidate in simulation_manager.simulation.ground_object_domains
                if getattr(candidate, "type", None) == "ground_station"
            ),
            None,
        )

    if domain is None:
        domain = GroundObjectDomain(
            identifier=ground_object.domain or "default",
            type="ground_station",
            ground_to_space_connections_strategy="best-angle-until-disconnection",
        )
        simulation_manager.add_ground_object_domain(domain)

    ground_object.ground_object_domain = domain


def add_custom_ground_station(
    ground_station: SatComGroundStation, simulation_manager: SimulationManager
) -> None:
    """Add a ground station to an existing simulation manager.

    The ground station is appended to the manager and attached to a compatible
    ground-object domain. Automatic ground-station links are **not** refreshed;
    call `simulation_manager.update_ground_station_links()` after all stations
    have been added.

    Args:
        ground_station: GroundStation instance created by create_custom_ground_station.
        simulation_manager: Active SimulationManager to receive the ground station.
    """
    _attach_ground_object_domain(ground_station, simulation_manager)
    simulation_manager.add_ground_station(ground_station)
    logging.info(f"Custom ground station {ground_station.topology_uniq_id} added")


def _find_satellite_by_name(
    name: str, simulation_manager: SimulationManager
) -> SatComSatellite:
    """Return the satellite whose ``satellite_name`` matches *name*.

    Args:
        name: Satellite name to look up.
        simulation_manager: Simulation manager to search.

    Returns:
        The matching satellite.

    Raises:
        ValueError: If no satellite with that name exists.
    """
    for satellite in simulation_manager.get_satellites():
        if getattr(satellite, "satellite_name", None) == name:
            return satellite
    raise ValueError(f"Satellite with name '{name}' not found")


def _find_ground_station_by_name(
    name: str, simulation_manager: SimulationManager
) -> SatComGroundStation:
    """Return the ground station whose ``label`` matches *name*.

    Args:
        name: Ground-station label to look up.
        simulation_manager: Simulation manager to search.

    Returns:
        The matching ground station.

    Raises:
        ValueError: If no ground station with that label exists.
    """
    for ground_station in simulation_manager.get_ground_stations():
        if getattr(ground_station, "label", None) == name:
            return ground_station
    raise ValueError(f"Ground station with name '{name}' not found")


def _find_ground_object_by_name(
    name: str, simulation_manager: SimulationManager
) -> SatComGroundObject:
    """Return the ground object (station or terminal) whose ``label`` matches.

    Args:
        name: Ground-object label to look up.
        simulation_manager: Simulation manager to search.

    Returns:
        The matching ground object.

    Raises:
        ValueError: If no ground object with that label exists.
    """
    for ground_object in simulation_manager.get_all_ground_objects():
        if getattr(ground_object, "label", None) == name:
            return ground_object
    raise ValueError(f"Ground object with name '{name}' not found")
