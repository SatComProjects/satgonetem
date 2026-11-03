"""Integration tests for first-class PointOfPresence support."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectData,
    GroundObjectProperty,
)
from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import (
    create_and_load_simulation,
    create_test_project,
)

START = "01/01/2024 00:00:00"


def _project():
    project = create_test_project(start_date=START)
    project.ground_objects_properties.append(
        GroundObjectProperty(
            identifier="Points of Presence",
            type="point_of_presence",
            ground_objects=[
                GroundObjectData(
                    id=0,
                    label="PoP0",
                    latitude=50.0,
                    longitude=8.0,
                    altitude=0.0,
                )
            ],
            connectivity_properties=GroundConnectivityProperty(
                ground_to_space_connections_strategy="everything-visible",
                elevation_above_horizon=10,
                maximum_connected_satellites=0,
            ),
        )
    )
    return project


def _manager() -> TopologyManager:
    return TopologyManager.from_satcom(
        _project(),
        NetworkConfig(update_time=5),
    )


def test_point_of_presence_is_a_first_class_node():
    manager = _manager()
    points = manager.get_point_of_presences()

    assert len(points) == 1
    point = points[0]
    assert point.name.startswith("Pop")
    assert point.type == "PointOfPresence"
    assert point.loopback.ipv4 is not None
    assert point.address_block is not None
    assert manager.get_topology_summary()["points_of_presence"] == 1


def test_ground_object_link_is_created_and_in_graph():
    manager = _manager()
    gs = next(
        node
        for node in manager.ground_stations.values()
        if node.type == "GroundStation"
    )
    pop = manager.get_point_of_presences()[0]

    manager.simulation_manager.create_and_add_ground_object_link_connection(
        gs.satcom_object, pop.satcom_object
    )
    manager.update_simulation(advance_timestep=False)

    link = next(
        link
        for link in manager.links.values()
        if link.type == "GroundObjectLink"
    )
    assert {link.source, link.target} == {gs, pop}
    assert manager.get_current_graph().has_edge(
        gs.satcom_object.topology_uniq_id,
        pop.satcom_object.topology_uniq_id,
    )


def test_point_of_presence_survives_runtime_state_round_trip():
    project = _project()
    sim_manager = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )
    manager = TopologyManager.from_simulation_manager(
        sim_manager, NetworkConfig(update_time=5)
    )
    gs = next(
        node
        for node in manager.ground_stations.values()
        if node.type == "GroundStation"
    )
    pop = manager.get_point_of_presences()[0]
    manager.simulation_manager.create_and_add_ground_object_link_connection(
        gs.satcom_object, pop.satcom_object
    )
    manager.update_simulation(advance_timestep=False)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "with_pop.json"
        manager.to_file(str(path))
        restored = TopologyManager.from_file(str(path))

    assert [node.type for node in restored.get_point_of_presences()] == [
        "PointOfPresence"
    ]
    assert any(
        link.type == "GroundObjectLink" for link in restored.links.values()
    )


def test_launcher_serializes_pop_prefix():
    pop = type(
        "Pop",
        (),
        {"id": 5, "name": "Pop5", "image": "", "type": "PointOfPresence"},
    )()
    text = GoNetEmLauncher._write_ground_stations([pop], "docker.gndstation")
    assert " Pop5:" in text


def test_reconciliation_handles_ground_object_link_creation():
    project = _project()
    manager = TopologyManager.from_satcom(
        project,
        NetworkConfig(
            update_time=5,
            topology_events=True,
            event_reconcile_interval=1,
        ),
    )
    gs = next(
        node
        for node in manager.ground_stations.values()
        if node.type == "GroundStation"
    )
    pop = manager.get_point_of_presences()[0]
    gol = manager.simulation_manager.create_and_add_ground_object_link_connection(
        gs.satcom_object, pop.satcom_object
    )
    manager.next_step()

    key = frozenset((gs.name, pop.name))
    assert key in manager.links

