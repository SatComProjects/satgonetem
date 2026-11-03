"""Tests for the Phase 5 metadata factories and event factories."""

from __future__ import annotations

from satgonetem.models.satcom_extensions import (
    EmulatorGroundStation,
    EmulatorSatellite,
    MetadataGSLEventFactory,
    MetadataISLEventFactory,
    emulator_event_factories,
    emulator_topology_object_factories,
)
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import create_test_project

START = "01/01/2024 00:00:00"


def _manager(**kwargs) -> TopologyManager:
    return TopologyManager.from_satcom(
        create_test_project(start_date=START),
        NetworkConfig(update_time=5),
        **kwargs,
    )


def test_emulator_factories_attach_identity_and_address_metadata():
    manager = _manager(
        topology_object_factories=emulator_topology_object_factories(),
    )
    satellite = next(iter(manager.simulation_manager.get_satellites()))
    ground_station = next(iter(manager.simulation_manager.get_ground_stations()))

    assert isinstance(satellite, EmulatorSatellite)
    assert satellite.emulator_id == 0
    assert satellite.container_name == "Sat0"
    assert satellite.address_block is not None

    assert isinstance(ground_station, EmulatorGroundStation)
    assert ground_station.container_name.startswith("Gnd")
    assert ground_station.address_block is not None


def test_metadata_event_factories_add_container_names():
    manager = _manager(
        topology_object_factories=emulator_topology_object_factories(),
        event_factories=emulator_event_factories(),
    )
    isl = next(iter(manager.simulation_manager.get_inter_satellites_links()))
    gsl = next(iter(manager.simulation_manager.get_ground_stations_links()))

    isl_event = MetadataISLEventFactory().created(isl)
    gsl_event = MetadataGSLEventFactory().created(gsl)

    assert isl_event.satellite_a_container.startswith("Sat")
    assert isl_event.satellite_b_container.startswith("Sat")
    assert gsl_event.satellite_container.startswith("Sat")
    assert gsl_event.ground_station_container.startswith("Gnd")


def test_custom_factories_work_with_event_mode():
    manager = _manager(
        topology_object_factories=emulator_topology_object_factories(),
        event_factories=emulator_event_factories(),
    )
    manager.topology_events = True
    manager.event_reconcile_interval = 0
    manager.enable_event_sync()

    manager.next_step()

    assert manager.get_last_stage_timings()["total"] >= 0.0
    assert manager._event_collector is not None
