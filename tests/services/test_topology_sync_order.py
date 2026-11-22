"""Regression test for delete-before-add link operation ordering."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sat_com_application.time_managers import SimulationTimeManager

from satgonetem.services.mixins.topology_sync import TopologySyncMixin
from satgonetem.services.topology_satcom import TopologyManager

_START = datetime(2024, 1, 1, 0, 0, 0)
_END = datetime(2024, 1, 2, 0, 0, 0)

_EMPTY_LINK_STATS = {
    "links_to_add": [],
    "links_to_update": [],
    "added_count": 0,
    "updated_count": 0,
    "deleted_count": 0,
    "add_time_total": 0.0,
    "add_time_wall": 0.0,
    "add_time_per_link": 0.0,
    "update_time_total": 0.0,
    "update_time_wall": 0.0,
    "update_time_per_link": 0.0,
    "delete_time_total": 0.0,
    "delete_time_wall": 0.0,
    "delete_time_per_link": 0.0,
}


def _topology_with_real_tick(current_time_step: int = 3):
    """Return a harness whose time update uses the real SimulationTimeManager."""
    time_manager = SimulationTimeManager(_START, _END)
    topology = MagicMock()
    topology.simulation_manager = SimpleNamespace(time_manager=time_manager)
    topology.update_time = 60
    topology.current_time_step = current_time_step
    topology._update_simulation_manager_time.side_effect = lambda: (
        TopologyManager._update_simulation_manager_time(topology)
    )
    topology._sync_links.return_value = {
        "links_to_add": [],
        "links_to_update": [],
        "links_to_remove": [],
    }
    topology.bulk_link_operations.return_value = dict(_EMPTY_LINK_STATS)
    return topology, time_manager


def test_update_simulation_records_last_link_operation_stats():
    topology = MagicMock()
    topology.simulation_manager = object()
    topology.current_time_step = 1
    topology.links = {}
    topology._sync_links.return_value = {
        "links_to_add": [],
        "links_to_update": [],
        "links_to_remove": [],
    }
    topology.bulk_link_operations.return_value = dict(_EMPTY_LINK_STATS)

    TopologySyncMixin.update_simulation(topology, advance_timestep=False)

    assert topology.last_link_operation_stats["added_count"] == 0
    assert topology.last_link_operation_stats["updated_count"] == 0
    assert topology.last_link_operation_stats["deleted_count"] == 0


def test_update_simulation_deletes_before_adding_links():
    stale = SimpleNamespace(to_remove=True)
    topology = MagicMock()
    topology.simulation_manager = object()
    topology.current_time_step = 1
    topology.links = {"stale": stale}
    topology._sync_links.return_value = {
        "links_to_add": [],
        "links_to_update": [],
        "links_to_remove": [stale],
    }

    calls = []

    def fake_bulk(**kwargs):
        calls.append(("bulk", kwargs))
        if kwargs.get("to_add") is False and kwargs.get("to_update") is False:
            return {
                "deleted_count": 1,
                "delete_time_total": 0.0,
                "delete_time_wall": 0.0,
                "delete_time_per_link": 0.0,
            }
        return {
            "added_count": 0,
            "updated_count": 0,
            "links_to_add": [],
            "add_time_total": 0.0,
            "add_time_wall": 0.0,
            "add_time_per_link": 0.0,
            "update_time_total": 0.0,
            "update_time_wall": 0.0,
            "update_time_per_link": 0.0,
            "deleted_count": 0,
            "delete_time_total": 0.0,
            "delete_time_wall": 0.0,
            "delete_time_per_link": 0.0,
        }

    def fake_local(**kwargs):
        calls.append(("local", kwargs))

    topology.bulk_link_operations.side_effect = fake_bulk
    topology._perform_local_link_operations.side_effect = fake_local

    TopologySyncMixin.update_simulation(topology, advance_timestep=False)

    assert calls == [
        (
            "bulk",
            {
                "to_add": False,
                "to_update": False,
                "links_to_add": [],
                "links_to_update": [],
                "links_to_remove": [stale],
            },
        ),
        (
            "local",
            {
                "to_add": False,
                "to_update": False,
                "links_to_add": [],
                "links_to_update": [],
                "links_to_remove": [stale],
            },
        ),
        (
            "bulk",
            {
                "to_del": False,
                "links_to_add": [],
                "links_to_update": [],
                "links_to_remove": [],
            },
        ),
        (
            "local",
            {
                "to_del": False,
                "links_to_add": [],
                "links_to_update": [],
                "links_to_remove": [],
            },
        ),
    ]
    topology._update_routing_after_link_changes.assert_called_once()


def test_step_counter_tracks_time_consumed_by_a_failing_tick():
    """tick() advances time before actions; a failing action still consumes it."""
    topology, time_manager = _topology_with_real_tick()

    def failing_action():
        raise RuntimeError("tick failed")

    time_manager.register_action(failing_action)

    with pytest.raises(RuntimeError, match="tick failed"):
        TopologySyncMixin.update_simulation(topology)

    assert time_manager.get_current_time() == _START + timedelta(seconds=60)
    assert topology.current_time_step == 4


def test_successful_tick_advances_the_counter_exactly_once():
    topology, time_manager = _topology_with_real_tick()

    TopologySyncMixin.update_simulation(topology)

    assert time_manager.get_current_time() == _START + timedelta(seconds=60)
    assert topology.current_time_step == 4
