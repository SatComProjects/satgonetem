"""Persistence tests for TopologyManager runtime-state round-trips.

A topology built from a SimulationManager (rather than a SimulationProperty)
is saved through ``to_file`` by serialising its runtime state.  Reloading it
must restore the simulation window and the programmatically added nodes.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from sat_com_model.models import PointOfPresence, UserTerminal

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import (
    add_custom_ground_station,
    create_and_load_simulation,
    create_custom_ground_station,
    create_custom_satellite,
)
from satgonetem.utils.project_fixtures import create_empty_project

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)

START_DATE = datetime(2024, 1, 1, 0, 0, 0)
END_DATE = datetime(2024, 1, 1, 1, 0, 0)

_ORIGINAL_DATA_FILE = "/tmp/ground_stations.txt"
_BENIGN_CSV = "0,OnlyStation,0.0,0.0,0.0\n"
_OTHER_CSV = "0,OtherStation,1.0,1.0,0.0\n"

_CUSTOM_SATELLITE = {
    "name": "AuditSat-001",
    "tle_line1": "1 25544U 98067A   24150.50000000  .00020000  00000-0  28000-4 0  9999",
    "tle_line2": "2 25544  51.6416 247.4627 0006703 130.5360 229.5775 15.509955193 12345",
}


@pytest.fixture
def runtime_topology():
    """Return a manager whose state exists only at runtime (no SimulationProperty)."""
    project = create_empty_project(
        simulation_name="RuntimeRoundTrip",
        start_date=START_DATE.strftime("%d/%m/%Y %H:%M:%S"),
        end_date=END_DATE.strftime("%d/%m/%Y %H:%M:%S"),
    )
    sim_manager = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )
    create_custom_satellite(_CUSTOM_SATELLITE, sim_manager, object_id=7)
    ground_station = create_custom_ground_station(
        "AuditGS", 48.0, 2.0, 0.0, object_id=9
    )
    add_custom_ground_station(ground_station, sim_manager)
    return TopologyManager.from_simulation_manager(sim_manager)


def test_runtime_state_roundtrip_preserves_dates_and_nodes(tmp_path, runtime_topology):
    assert runtime_topology.simulation_property is None
    path = tmp_path / "runtime_topology.json"

    runtime_topology.to_file(str(path))
    reloaded = TopologyManager.from_file(str(path))

    assert reloaded.simulation_manager.time_manager.start_date == START_DATE
    assert reloaded.simulation_manager.time_manager.end_date == END_DATE
    assert [
        sat.satellite_name for sat in reloaded.simulation_manager.get_satellites()
    ] == ["AuditSat-001"]
    assert [gs.label for gs in reloaded.simulation_manager.get_ground_stations()] == [
        "AuditGS"
    ]


def _write_payload(path: Path, basename: object, content: str = _BENIGN_CSV) -> None:
    """Write a topology payload whose embedded ground file has *basename*."""
    payload = json.loads(Path(TEST_TOPOLOGY).read_text())
    payload["ground_files"] = {
        _ORIGINAL_DATA_FILE: {"content": content, "basename": basename}
    }
    path.write_text(json.dumps(payload))


class TestGroundFileRestorePaths:
    """Embedded ground station files must only be restored inside a fresh dir."""

    def test_rejects_absolute_basename_and_writes_nothing(self, tmp_path):
        escape_target = tmp_path / "absolute_escape.csv"
        payload_path = tmp_path / "topology.json"
        _write_payload(payload_path, str(escape_target))

        with pytest.raises(ValueError, match="ground file basename"):
            TopologyManager.from_file(str(payload_path))

        assert not escape_target.exists()

    @pytest.mark.parametrize(
        "basename", ["../escaped.csv", "sub/escaped.csv", "", ".", ".."]
    )
    def test_rejects_non_plain_basenames_and_writes_nothing(self, tmp_path, basename):
        payload_path = tmp_path / "topology.json"
        _write_payload(payload_path, basename)

        with pytest.raises(ValueError, match="ground file basename"):
            TopologyManager.from_file(str(payload_path))

        assert not (tmp_path / "escaped.csv").exists()
        assert not (tmp_path / "sub" / "escaped.csv").exists()

    def test_benign_ground_files_roundtrip(self, tmp_path):
        topology = TopologyManager.from_file(TEST_TOPOLOGY)
        payload_path = tmp_path / "topology.json"

        topology.to_file(str(payload_path))
        reloaded = TopologyManager.from_file(str(payload_path))

        assert [
            gs.label for gs in reloaded.simulation_manager.get_ground_stations()
        ] == ["Berlin", "London", "Paris", "Rome", "Madrid"]
        data_file = reloaded.simulation_property.ground_objects_properties[0].data_file
        assert os.path.isfile(data_file)
        assert Path(data_file).name == "ground_stations.txt"

    def test_same_stem_loads_use_distinct_restore_directories(self, tmp_path):
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        _write_payload(first_dir / "topology.json", "ground_stations.txt", _BENIGN_CSV)
        _write_payload(second_dir / "topology.json", "ground_stations.txt", _OTHER_CSV)

        first = TopologyManager.from_file(str(first_dir / "topology.json"))
        second = TopologyManager.from_file(str(second_dir / "topology.json"))

        first_data_file = Path(
            first.simulation_property.ground_objects_properties[0].data_file
        )
        second_data_file = Path(
            second.simulation_property.ground_objects_properties[0].data_file
        )
        assert first_data_file.parent != second_data_file.parent
        assert first_data_file.read_text() == _BENIGN_CSV
        assert second_data_file.read_text() == _OTHER_CSV


def test_runtime_state_roundtrip_preserves_user_terminals_and_pops(
    tmp_path, runtime_topology
):
    """User terminals, points of presence and their links must survive a reload."""
    sim_manager = runtime_topology.simulation_manager
    satellite = sim_manager.get_satellites()[0]
    ground_station = sim_manager.get_ground_stations()[0]

    user_terminal = UserTerminal(label="AuditUT", object_id=11, domain="public")
    user_terminal.set_position(latitude=40.5, longitude=-3.5, altitude=120.0)
    sim_manager.add_user_terminals(user_terminal)
    sim_manager.create_and_add_user_terminal_link_connection(satellite, user_terminal)

    point_of_presence = PointOfPresence(label="AuditPoP", object_id=12, domain="public")
    point_of_presence.set_position(latitude=41.5, longitude=-4.5, altitude=220.0)
    sim_manager.add_point_of_presence(point_of_presence)
    sim_manager.create_and_add_ground_object_link_connection(
        point_of_presence, ground_station
    )

    path = tmp_path / "runtime_ut_pop.json"
    runtime_topology.to_file(str(path))
    reloaded = TopologyManager.from_file(str(path))

    reloaded_manager = reloaded.simulation_manager
    user_terminals = reloaded_manager.get_user_terminals()
    assert [(ut.label, ut.object_id, ut.domain) for ut in user_terminals] == [
        ("AuditUT", 11, "public")
    ]
    assert user_terminals[0].spatial_position_info.latitude == pytest.approx(40.5)
    assert user_terminals[0].spatial_position_info.longitude == pytest.approx(-3.5)
    assert user_terminals[0].spatial_position_info.altitude == pytest.approx(120.0)

    pops = reloaded_manager.get_point_of_presences()
    assert [(pop.label, pop.object_id, pop.domain) for pop in pops] == [
        ("AuditPoP", 12, "public")
    ]
    assert pops[0].spatial_position_info.latitude == pytest.approx(41.5)
    assert pops[0].spatial_position_info.longitude == pytest.approx(-4.5)
    assert pops[0].spatial_position_info.altitude == pytest.approx(220.0)

    user_terminal_links = reloaded_manager.get_user_terminal_links()
    assert [
        (link.source.satellite_name, link.destination.label)
        for link in user_terminal_links
    ] == [("AuditSat-001", "AuditUT")]
    ground_object_links = reloaded_manager.get_static_ground_object_links()
    assert [
        (link.source.label, link.destination.label) for link in ground_object_links
    ] == [("AuditPoP", "AuditGS")]

    satellite_before = reloaded_manager.get_satellites()[0]
    position_before = (
        satellite_before.spatial_position_info.latitude,
        satellite_before.spatial_position_info.longitude,
        satellite_before.spatial_position_info.altitude,
    )
    time_before = reloaded.get_current_time()
    step_before = reloaded.get_current_time_step()

    reloaded.update_simulation()

    assert reloaded.get_current_time() > time_before
    assert reloaded.get_current_time_step() == step_before + 1
    satellite_after = reloaded_manager.get_satellites()[0]
    position_after = (
        satellite_after.spatial_position_info.latitude,
        satellite_after.spatial_position_info.longitude,
        satellite_after.spatial_position_info.altitude,
    )
    assert position_after != position_before

    # Restored user terminals and points of presence need a ground-object
    # domain, otherwise the next tick raises AttributeError.
    for ground_object in (user_terminals[0], pops[0]):
        assert ground_object.ground_object_domain is not None
        assert ground_object.ground_object_domain.type == "ground_station"


def test_runtime_state_roundtrip_preserves_link_impairments_and_addressability(
    tmp_path,
):
    """Per-link impairment/admin/capacity-mode and addressability survive reload."""
    project = create_empty_project(
        simulation_name="RuntimeLinkState",
        start_date=START_DATE.strftime("%d/%m/%Y %H:%M:%S"),
        end_date=END_DATE.strftime("%d/%m/%Y %H:%M:%S"),
    )
    sim_manager = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )
    create_custom_satellite(_CUSTOM_SATELLITE, sim_manager, object_id=7)
    ground_station = create_custom_ground_station(
        "AuditGS", 48.0, 2.0, 0.0, object_id=9
    )
    add_custom_ground_station(ground_station, sim_manager)
    satellite = sim_manager.get_satellites()[0]
    sim_manager.create_and_add_ground_station_link_connection(satellite, ground_station)

    topology = TopologyManager.from_simulation_manager(sim_manager)
    link = next(
        candidate
        for candidate in topology.get_links()
        if candidate.type == "GroundStationLink"
    )
    link.loss = 12.5
    link.jitter = 7
    link.admin_up = False
    link.use_budget = True
    topology.get_satellites()[0].set_addressable(True)

    path = tmp_path / "runtime_link_state.json"
    topology.to_file(str(path))
    payload = json.loads(path.read_text())

    satellite_record = payload["runtime_state"]["satellites"][0]
    link_record = payload["runtime_state"]["links"]["ground_station"][0]
    assert satellite_record["addressable"] is True
    assert link_record["loss"] == 12.5
    assert link_record["jitter"] == 7
    assert link_record["admin_up"] is False
    assert link_record["use_budget"] is True

    reloaded = TopologyManager.from_file(str(path))
    restored_link = next(
        candidate
        for candidate in reloaded.get_links()
        if candidate.type == "GroundStationLink"
    )

    assert reloaded.simulation_manager.time_manager.start_date == START_DATE
    assert reloaded.get_satellites()[0].is_addressable() is True
    assert restored_link.loss == 12.5
    assert restored_link.jitter == 7
    assert restored_link.admin_up is False
    assert restored_link.use_budget is True
    assert restored_link.peer_interfaces
    assert all(not interface.is_active for interface in restored_link.peer_interfaces)


def test_runtime_state_roundtrip_keeps_ipv6_enabled(tmp_path, runtime_topology):
    """A topology saved with IPv6 enabled reloads with the plan rebuilt."""
    path = tmp_path / "runtime_ipv6.json"

    runtime_topology.to_file(str(path), network_config=NetworkConfig(ipv6_enabled=True))
    reloaded = TopologyManager.from_file(str(path))

    assert reloaded.ipv6_enabled is True
    assert reloaded.address_plan_v6 is not None
    for node in list(reloaded.get_satellites()) + list(reloaded.get_ground_stations()):
        assert node.address_block_v6 is not None
        assert node.address_block_v6.prefixlen == 64
        assert node.loopback.ipv6_prefix_len == 128


def test_runtime_state_from_file_override_enables_ipv6(tmp_path, runtime_topology):
    """An explicit config wins wholesale on the runtime-state load path."""
    path = tmp_path / "runtime_ipv6_disabled.json"

    runtime_topology.to_file(
        str(path), network_config=NetworkConfig(ipv6_enabled=False)
    )
    reloaded = TopologyManager.from_file(
        str(path), network_config=NetworkConfig(ipv6_enabled=True)
    )

    assert reloaded.ipv6_enabled is True
    assert reloaded.address_plan_v6 is not None
    assert [sat.address_block_v6 is not None for sat in reloaded.get_satellites()] == [
        True
    ]
