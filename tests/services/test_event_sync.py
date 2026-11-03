"""Integration tests for the V5 event-driven link sync path."""

from __future__ import annotations

import asyncio
from itertools import combinations
from types import SimpleNamespace

import pytest

from sat_com_application.event_bus.events import EventType

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.utils.project_builder import create_test_project

PROJECT_START = "01/01/2024 00:00:00"


def _project():
    return create_test_project(start_date=PROJECT_START)


def _event_manager(reconcile_interval: int = 0) -> TopologyManager:
    return TopologyManager.from_satcom(
        _project(),
        NetworkConfig(
            update_time=5,
            topology_events=True,
            event_reconcile_interval=reconcile_interval,
        ),
    )


def _polling_manager() -> TopologyManager:
    return TopologyManager.from_satcom(
        _project(),
        NetworkConfig(update_time=5, topology_events=False),
    )


def _disconnected_pair(manager: TopologyManager):
    satellites = list(manager.simulation_manager.get_satellites())
    for satellite_a, satellite_b in combinations(satellites, 2):
        key = frozenset(
            (
                f"Sat{satellite_a.topology_uniq_id}",
                f"Sat{satellite_b.topology_uniq_id}",
            )
        )
        if key not in manager.links:
            return satellite_a, satellite_b, key
    raise AssertionError("test topology has no disconnected satellite pair")


def test_event_mode_attaches_bus_and_collector():
    manager = _event_manager()

    assert manager.event_bus is not None
    assert manager._event_collector is not None
    assert manager.simulation_manager.event_bus is manager.event_bus


def test_event_and_polling_paths_reach_the_same_link_state():
    polling = _polling_manager()
    event = _event_manager()

    for _ in range(10):
        polling.next_step()
        event.next_step()

    assert set(polling.links) == set(event.links)
    for key in polling.links:
        assert polling.links[key].is_active == event.links[key].is_active
        assert polling.links[key].delay == event.links[key].delay
        assert polling.links[key].distance == pytest.approx(
            event.links[key].distance
        )

    polling_graph = polling.get_current_graph()
    event_graph = event.get_current_graph()
    assert set(polling_graph.edges) == set(event_graph.edges)
    for u, v in polling_graph.edges:
        assert polling_graph[u][v]["weight"] == pytest.approx(
            event_graph[u][v]["weight"]
        )


def test_event_key_maps_gsl_and_ignores_utl():
    manager = _event_manager()
    satellite = next(iter(manager.satellites.values()))
    ground_station = next(iter(manager.ground_stations.values()))
    gsl_event = SimpleNamespace(
        type=EventType.GSL_CREATED,
        satellite_id=satellite.id,
        ground_station_id=ground_station.id,
    )
    utl_event = SimpleNamespace(type=EventType.UTL_CREATED)

    assert manager._link_event_key(gsl_event) == frozenset(
        (f"Sat{satellite.id}", f"Gnd{ground_station.id}")
    )
    assert manager._link_event_key(utl_event) is None


def test_created_link_event_adds_an_emulator_link():
    manager = _event_manager()
    satellite_a, satellite_b, key = _disconnected_pair(manager)

    manager.simulation_manager.create_and_add_inter_satellite_link_connection(
        satellite_a, satellite_b, distance_km=1_234.0
    )
    manager.update_simulation(advance_timestep=False)

    assert key in manager.links
    assert manager.links[key].distance == pytest.approx(1_234_000.0)
    assert manager.links[key].is_active is True


def test_removed_link_event_removes_an_emulator_link():
    manager = _event_manager()
    link = next(
        link for link in manager.links.values() if link.type == "InterSatelliteLink"
    )
    key = frozenset((link.source.name, link.target.name))

    manager.simulate_link_failure([link])

    assert key not in manager.links


def test_reconciliation_restores_a_missing_link():
    manager = _event_manager(reconcile_interval=1)
    key = next(iter(manager.links))
    manager.links.pop(key)

    manager.next_step()

    assert key in manager.links


def test_next_step_async_uses_worker_thread_and_applies_events():
    manager = _event_manager()
    link_count = len(manager.links)

    elapsed = asyncio.run(manager.next_step_async())

    assert elapsed >= 0.0
    assert manager.current_time_step == 1
    assert len(manager.links) == link_count
