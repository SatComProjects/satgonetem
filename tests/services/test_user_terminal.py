"""Integration tests for first-class user terminal support."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectData,
    GroundObjectProperty,
)
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import create_test_project

START = "01/01/2024 00:00:00"


def _project():
    project = create_test_project(start_date=START)
    project.walker_shells[0].ground_object_white_list.append("User Terminals")
    project.ground_objects_properties.append(
        GroundObjectProperty(
            identifier="User Terminals",
            type="user_terminal",
            ground_objects=[
                GroundObjectData(
                    id=0,
                    label="UT0",
                    latitude=48.85,
                    longitude=2.35,
                    altitude=0.0,
                )
            ],
            connectivity_properties=GroundConnectivityProperty(
                ground_to_space_connections_strategy=(
                    "best-angle-until-disconnection"
                ),
                elevation_above_horizon=10,
                maximum_satellite_range_distance=1_500.0,
                shells_whitelist=["LEO"],
                maximum_connected_satellites=1,
            ),
        )
    )
    return project


def _manager() -> TopologyManager:
    return TopologyManager.from_satcom(
        _project(),
        NetworkConfig(update_time=5),
    )


def test_user_terminal_is_a_first_class_node():
    manager = _manager()
    terminals = manager.get_user_terminals()

    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.name.startswith("Usr")
    assert terminal.type == "UserTerminal"
    assert terminal.loopback.ipv4 is not None
    assert terminal.address_block is not None

    links = [
        link for link in manager.links.values() if link.type == "UserTerminalLink"
    ]
    assert len(links) == 1
    assert terminal in {links[0].source, links[0].target}


def test_user_terminal_is_in_routing_graph_and_summary():
    manager = _manager()

    graph = manager.get_current_graph()
    terminal = manager.get_user_terminals()[0]
    assert terminal.satcom_object.topology_uniq_id in graph

    summary = manager.get_topology_summary()
    assert summary["user_terminals"] == 1
    assert summary["ground_stations"] == 5


def test_user_terminal_survives_runtime_state_round_trip():
    manager = _manager()

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "with_ut.json"
        manager.to_file(str(path))
        restored = TopologyManager.from_file(str(path))

    assert [node.type for node in restored.get_user_terminals()] == ["UserTerminal"]
    assert any(
        link.type == "UserTerminalLink" for link in restored.links.values()
    )


def test_event_mode_handles_user_terminal_link_removal():
    manager = TopologyManager.from_satcom(
        _project(),
        NetworkConfig(
            update_time=5,
            topology_events=True,
            event_reconcile_interval=0,
        ),
    )
    link = next(
        link for link in manager.links.values() if link.type == "UserTerminalLink"
    )
    key = frozenset((link.source.name, link.target.name))

    manager.simulate_link_failure([link])

    assert key not in manager.links
