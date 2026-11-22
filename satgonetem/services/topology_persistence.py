"""File and runtime-state persistence for :class:`TopologyManager`.

The codec is deliberately facade-free: it reads and writes a duck-typed
manager, returns :class:`SimulationManager`/:class:`SimulationProperty`, and
never imports ``topology_satcom``.  That keeps the persistence module leaf-ward
and lets it run against bare test instances.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from typing import Any

from sat_com_application.simulation_manager import SimulationManager
from sat_com_builder.models import SimulationProperty
from sat_com_model.models import PointOfPresence, UserTerminal

from satgonetem.services.network_config import NetworkConfig, config_from_instance
from satgonetem.utils.project_builder import (
    _attach_ground_object_domain,
    add_custom_ground_station,
    create_and_load_simulation,
    create_custom_ground_station,
    create_custom_satellite,
)
from satgonetem.utils.project_fixtures import create_empty_project

# Serialised simulation dates must use the format accepted by
# satgonetem.utils.project_fixtures.create_empty_project() on restore.
RUNTIME_DATE_FORMAT = "%d/%m/%Y %H:%M:%S"


def ground_file_restore_path(ground_dir: str, basename: object) -> str:
    """Return a safe restore path for an embedded ground station data file.

    Only plain file names are accepted. The persisted ``basename`` must not be
    absolute, empty, or carry directory components such as ``..``.

    Args:
        ground_dir: Private directory created for this restore.
        basename: ``ground_files[<original path>]["basename"]`` from the payload.

    Returns:
        Absolute path inside *ground_dir* where the file can be written.

    Raises:
        ValueError: If basename is not a plain file name or the resolved path
            would leave *ground_dir*.
    """
    if not isinstance(basename, str) or not basename:
        raise ValueError(f"Invalid ground file basename: {basename!r}")

    name = os.path.basename(basename)
    if os.path.isabs(basename) or name != basename or name in {os.curdir, os.pardir}:
        raise ValueError(f"Invalid ground file basename: {basename!r}")

    root = os.path.realpath(ground_dir)
    target = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"Invalid ground file basename: {basename!r}")

    return target


def serialize_runtime_state(manager: Any) -> dict[str, Any]:
    """Extract satellites, ground objects and links from a live SimulationManager.

    Args:
        manager: Object exposing ``simulation_manager``.

    Returns:
        A dictionary that can be passed to :func:`restore_runtime_state`.
    """
    state: dict[str, Any] = {
        "simulation_name": getattr(
            manager.simulation_manager, "project_name", "Constellation"
        ),
        "start_date": manager.simulation_manager.time_manager.start_date.strftime(
            RUNTIME_DATE_FORMAT
        ),
        "end_date": manager.simulation_manager.time_manager.end_date.strftime(
            RUNTIME_DATE_FORMAT
        ),
        "satellites": [],
        "ground_stations": [],
        "user_terminals": [],
        "points_of_presence": [],
        "links": {
            "inter_satellite": [],
            "ground_station": [],
            "user_terminal": [],
            "ground_object": [],
        },
    }

    for sat in manager.simulation_manager.get_satellites():
        mm = sat.get_movement_model()
        state["satellites"].append(
            {
                "name": sat.satellite_name,
                "tle_line1": getattr(mm, "line1", ""),
                "tle_line2": getattr(mm, "line2", ""),
                "object_id": sat.object_id,
                "domain": sat.domain,
            }
        )

    for gs in manager.simulation_manager.get_ground_stations():
        pos = gs.spatial_position_info
        state["ground_stations"].append(
            {
                "label": gs.label,
                "latitude": pos.latitude,
                "longitude": pos.longitude,
                "altitude": pos.altitude,
                "object_id": gs.object_id,
                "domain": gs.domain,
            }
        )

    for ut in manager.simulation_manager.get_user_terminals():
        pos = ut.spatial_position_info
        state["user_terminals"].append(
            {
                "label": ut.label,
                "latitude": pos.latitude,
                "longitude": pos.longitude,
                "altitude": pos.altitude,
                "object_id": ut.object_id,
                "domain": ut.domain,
            }
        )

    for pop in manager.simulation_manager.get_point_of_presences():
        pos = pop.spatial_position_info
        state["points_of_presence"].append(
            {
                "label": pop.label,
                "latitude": pos.latitude,
                "longitude": pos.longitude,
                "altitude": pos.altitude,
                "object_id": pop.object_id,
                "domain": pop.domain,
            }
        )

    for link in manager.simulation_manager.get_inter_satellites_links():
        direction = getattr(link.inter_satellite_direction, "name", "UNDEFINED")
        state["links"]["inter_satellite"].append(
            {
                "source": getattr(link.source, "satellite_name", ""),
                "destination": getattr(link.destination, "satellite_name", ""),
                "direction": direction,
            }
        )

    for link in manager.simulation_manager.get_ground_stations_links():
        src = getattr(link.source, "satellite_name", getattr(link.source, "label", ""))
        dst = getattr(
            link.destination,
            "satellite_name",
            getattr(link.destination, "label", ""),
        )
        state["links"]["ground_station"].append({"source": src, "destination": dst})

    for link in manager.simulation_manager.get_user_terminal_links():
        src = getattr(link.source, "satellite_name", getattr(link.source, "label", ""))
        dst = getattr(
            link.destination,
            "satellite_name",
            getattr(link.destination, "label", ""),
        )
        state["links"]["user_terminal"].append({"source": src, "destination": dst})

    for link in manager.simulation_manager.get_static_ground_object_links():
        src = getattr(link.source, "label", "")
        dst = getattr(link.destination, "label", "")
        state["links"]["ground_object"].append({"source": src, "destination": dst})

    return state


def save_to_file(
    manager: Any,
    path: str,
    network_config: NetworkConfig | None = None,
    serialize_state: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """Serialise *manager* and its network config to a JSON file.

    Args:
        manager: Object exposing ``simulation_property``, ``simulation_manager``
            and the serialization helpers.
        path: Filesystem path where the JSON file will be written.
        network_config: NetworkConfig to persist. If None, a NetworkConfig is
            built from the current instance attributes.
        serialize_state: Callable producing the runtime state for programmatic
            managers. It is only called when the manager has no
            ``simulation_property``, preserving the original ordering. When
            None, :func:`serialize_runtime_state` is used.

    Raises:
        OSError: If the file cannot be written or a ground station data file
            cannot be read.
    """
    if network_config is None:
        network_config = config_from_instance(manager)

    if manager.simulation_property is not None:
        sim_prop_data = manager.simulation_property.model_dump()
    else:
        sim_prop_data = dict(
            manager.simulation_manager.configuration.get("properties", {})
        )

    ground_files: dict[str, dict[str, str]] = {}
    for gop in sim_prop_data.get("ground_objects_properties", []):
        data_file = gop.get("data_file", "")
        if data_file and os.path.isfile(data_file):
            with open(data_file, encoding="utf-8") as fh:
                ground_files[data_file] = {
                    "content": fh.read(),
                    "basename": os.path.basename(data_file),
                }

    payload: dict[str, Any] = {
        "simulation_property": sim_prop_data,
        "network_config": network_config.__dict__,
        "ground_files": ground_files,
    }

    if manager.simulation_property is None:
        payload["runtime_state"] = (
            serialize_state()
            if serialize_state is not None
            else serialize_runtime_state(manager)
        )

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def restore_runtime_state(
    runtime_state: dict[str, Any], network_config: NetworkConfig
) -> SimulationManager:
    """Rebuild a SimulationManager from a saved runtime state.

    Malformed node or link records are logged and skipped; the rest of the
    topology is still restored.

    Args:
        runtime_state: Dictionary produced by :func:`serialize_runtime_state`.
        network_config: NetworkConfig to apply to the rebuilt manager.

    Returns:
        A loaded SimulationManager holding the restored objects.
    """
    empty_project = create_empty_project(
        simulation_name=runtime_state.get("simulation_name", "Constellation"),
        start_date=runtime_state.get("start_date", ""),
        end_date=runtime_state.get("end_date", ""),
    )
    sim_manager = create_and_load_simulation(
        empty_project.model_dump(), empty_project.simulation_name
    )

    for sat_data in runtime_state.get("satellites", []):
        create_custom_satellite(
            sat_data,
            sim_manager,
            object_id=sat_data.get("object_id", 0),
            domain=sat_data.get("domain", "public"),
        )

    for gs_data in runtime_state.get("ground_stations", []):
        gs = create_custom_ground_station(
            name=gs_data["label"],
            latitude=gs_data["latitude"],
            longitude=gs_data["longitude"],
            elevation_km=gs_data.get("altitude", 0) / 1000.0,
            object_id=gs_data.get("object_id", 0),
            domain=gs_data.get("domain", "public"),
        )
        add_custom_ground_station(gs, sim_manager)

    for ut_data in runtime_state.get("user_terminals", []):
        user_terminal = UserTerminal()
        user_terminal.label = ut_data["label"]
        user_terminal.object_id = ut_data.get("object_id", 0)
        user_terminal.domain = ut_data.get("domain", "public")
        user_terminal.set_position(
            latitude=ut_data["latitude"],
            longitude=ut_data["longitude"],
            altitude=ut_data.get("altitude", 0),
        )
        _attach_ground_object_domain(user_terminal, sim_manager)
        sim_manager.add_user_terminals(user_terminal)

    for pop_data in runtime_state.get("points_of_presence", []):
        point_of_presence = PointOfPresence()
        point_of_presence.label = pop_data["label"]
        point_of_presence.object_id = pop_data.get("object_id", 0)
        point_of_presence.domain = pop_data.get("domain", "public")
        point_of_presence.set_position(
            latitude=pop_data["latitude"],
            longitude=pop_data["longitude"],
            altitude=pop_data.get("altitude", 0),
        )
        _attach_ground_object_domain(point_of_presence, sim_manager)
        sim_manager.add_point_of_presence(point_of_presence)

    from sat_com_model.models import InterSatelliteLinkDirection

    links = runtime_state.get("links", {})
    for isl in links.get("inter_satellite", []):
        try:
            sat_a = next(
                s
                for s in sim_manager.get_satellites()
                if s.satellite_name == isl["source"]
            )
            sat_b = next(
                s
                for s in sim_manager.get_satellites()
                if s.satellite_name == isl["destination"]
            )
        except StopIteration:
            logging.warning(f"Failed to restore ISL: satellites not found {isl}")
            continue

        try:
            link = sim_manager.create_and_add_inter_satellite_link_connection(
                sat_a, sat_b
            )
            dir_name = isl.get("direction", "UNDEFINED")
            if dir_name == "ORBITAL":
                link.inter_satellite_direction = InterSatelliteLinkDirection.ORBITAL
            elif dir_name == "ADJACENT":
                link.inter_satellite_direction = InterSatelliteLinkDirection.ADJACENT
        except Exception as exc:
            logging.warning(f"Failed to restore ISL {isl}: {exc}")

    for gsl in links.get("ground_station", []):
        try:
            sat = next(
                s
                for s in sim_manager.get_satellites()
                if s.satellite_name == gsl["source"]
            )
            gs = next(
                g
                for g in sim_manager.get_ground_stations()
                if g.label == gsl["destination"]
            )
            sim_manager.create_and_add_ground_station_link_connection(sat, gs)
        except Exception as exc:
            logging.warning(f"Failed to restore GSL {gsl}: {exc}")

    for utl in links.get("user_terminal", []):
        try:
            sat = next(
                s
                for s in sim_manager.get_satellites()
                if s.satellite_name == utl["source"]
            )
            ut = next(
                u
                for u in sim_manager.get_user_terminals()
                if u.label == utl["destination"]
            )
            sim_manager.create_and_add_user_terminal_link_connection(sat, ut)
        except Exception as exc:
            logging.warning(f"Failed to restore user-terminal link {utl}: {exc}")

    for gol in links.get("ground_object", []):
        try:
            go_a = next(
                g
                for g in sim_manager.get_all_ground_objects()
                if g.label == gol["source"]
            )
            go_b = next(
                g
                for g in sim_manager.get_all_ground_objects()
                if g.label == gol["destination"]
            )
            sim_manager.create_and_add_ground_object_link_connection(go_a, go_b)
        except Exception as exc:
            logging.warning(f"Failed to restore ground-object link {gol}: {exc}")

    return sim_manager


def restore_simulation_property(
    payload: dict[str, Any], path: str
) -> SimulationProperty:
    """Restore embedded ground files and build the persisted SimulationProperty.

    Args:
        payload: Parsed ``to_file`` payload without a ``runtime_state`` block.
        path: Path of the payload file, used to derive the restore directory.

    Returns:
        The validated SimulationProperty with rewritten ``data_file`` paths.

    Raises:
        ValueError: If an embedded ground station file name is unsafe.
        OSError: If ground station data files cannot be written.
    """
    sim_prop_data: dict[str, Any] = payload["simulation_property"]
    ground_files: dict[str, dict[str, str]] = payload.get("ground_files", {})

    if ground_files:
        stem = os.path.splitext(os.path.basename(path))[0]
        ground_dir = tempfile.mkdtemp(prefix=f"{stem}_ground_files_")

        path_map: dict[str, str] = {}
        for original_path, file_info in ground_files.items():
            restored_path = ground_file_restore_path(
                ground_dir, file_info.get("basename")
            )
            with open(restored_path, "w", encoding="utf-8") as fh:
                fh.write(file_info["content"])
            path_map[original_path] = restored_path

        for gop in sim_prop_data.get("ground_objects_properties", []):
            orig = gop.get("data_file", "")
            if orig in path_map:
                gop["data_file"] = path_map[orig]

    return SimulationProperty.model_validate(sim_prop_data)
