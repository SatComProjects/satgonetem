"""Persistence tests for TopologyManager runtime-state round-trips.

A topology built from a SimulationManager (rather than a SimulationProperty)
is saved through ``to_file`` by serialising its runtime state.  Reloading it
must restore the simulation window and the programmatically added nodes.
"""

from datetime import datetime

import pytest

from satgonetem.services.topology_satcom import TopologyManager
from satgonetem.utils.project_builder import (
    add_custom_ground_station,
    create_and_load_simulation,
    create_custom_ground_station,
    create_custom_satellite,
)
from satgonetem.utils.project_fixtures import create_empty_project

START_DATE = datetime(2024, 1, 1, 0, 0, 0)
END_DATE = datetime(2024, 1, 1, 1, 0, 0)

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
