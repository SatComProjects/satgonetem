"""Build a topology from a scenario's Walker shells and ground stations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from typing import Any

from sat_com_builder.models import (
    GroundConnectivityProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty

from satgonetem.experiment.scenario import Scenario, ShellSpec
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import (
    GroundObject,
    GroundObjectFile,
    GroundStationEntry,
    create_empty_project,
)

__all__ = ["build_scenario_topology"]


def _walker_shell(
    spec: ShellSpec, ground_station_ids: list[str]
) -> WalkerShellProperty:
    return WalkerShellProperty(
        type=spec.pattern,
        constellation_property=WalkerConstellationProperty(
            identifier=spec.identifier,
            amount_of_orbit_plane=spec.planes,
            amount_of_satellite_per_orbit_plane=spec.satellites_per_plane,
            inclination=spec.inclination_deg,
            mean_revolution_per_day=spec.revs_per_day,
            phase_difference_between_satellites=spec.phase_difference,
        ),
        orbital_connectivity_property=OrbitalConnectivityProperty(
            adjacent_inter_satellite_shifting=0,
            maximum_inter_satellite_count=4,
            maximum_inter_satellite_range_distance=1500.0,
            maximum_ground_station_range=1500.0,
            maximum_user_terminal_range=1000.0,
            maximum_connected_ground_object=10000,
            maximum_connected_user_terminal=500,
            maximum_connected_ground_station=10,
        ),
        ground_object_white_list=ground_station_ids,
    )


def build_scenario_topology(
    scenario: Scenario,
    network_config: NetworkConfig | None = None,
) -> TopologyManager:
    """Build a topology from ``scenario.shells`` and ``scenario.ground_stations``.

    The generated project uses the same Walker-shell and ground-object
    builders as the documented Python API. Ground stations are assigned in
    scenario order, so they become ``Gnd0``, ``Gnd1``, ... in the emulated
    topology.
    """
    if scenario.topology is not None:
        raise ValueError("build_scenario_topology requires a shell-based scenario")
    if not scenario.shells:
        raise ValueError("scenario has no shells")

    now = datetime.now(timezone.utc)
    end = now + timedelta(seconds=max(600.0, scenario.duration_s + 60.0))
    project = create_empty_project(
        simulation_name=scenario.name,
        start_date=now.strftime("%d/%m/%Y %H:%M:%S"),
        end_date=end.strftime("%d/%m/%Y %H:%M:%S"),
    )
    payload: dict[str, Any] = project.model_dump()

    ground_identifier = "Ground Stations"
    ground_entries = [
        GroundStationEntry(
            index=index,
            name=station.name,
            latitude=station.latitude,
            longitude=station.longitude,
            elevation_km=station.elevation_km,
        )
        for index, station in enumerate(scenario.ground_stations)
    ]

    data_dir = TemporaryDirectory(prefix="satgonetem_scenario_")
    try:
        ground_properties: list[dict[str, Any]] = []
        if ground_entries:
            ground_file = GroundObjectFile(ground_identifier, ground_entries)
            ground_properties.append(
                GroundObject(
                    ground_file,
                    "ground_station",
                    GroundConnectivityProperty(
                        ground_to_space_connections_strategy=(
                            "best-angle-until-disconnection"
                        ),
                        elevation_above_horizon=10.0,
                        maximum_satellite_range_distance=1500.0,
                        shell_white_lists=[
                            shell.identifier for shell in scenario.shells
                        ],
                        maximum_connected_satellites=3,
                    ),
                ).to_dict(data_dir.name)
            )

        shell_ids = [ground_identifier] if ground_entries else []
        payload["ground_objects_properties"] = ground_properties
        payload["walker_shells"] = [
            _walker_shell(shell, shell_ids).model_dump() for shell in scenario.shells
        ]
        simulation_property = SimulationProperty.model_validate(payload)
        topology = TopologyManager.from_satcom(simulation_property, network_config)
    except BaseException:
        data_dir.cleanup()
        raise
    # Keep the generated ground-station CSV alive for as long as the
    # topology: to_file() embeds referenced data files, and a dangling path
    # would make the saved topology impossible to reload. The temporary
    # directory is removed when the topology is garbage-collected.
    topology._scenario_data_dir = data_dir
    return topology
