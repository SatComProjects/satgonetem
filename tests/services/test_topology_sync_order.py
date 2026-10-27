"""Regression test for delete-before-add link operation ordering."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from satgonetem.services.mixins.topology_sync import TopologySyncMixin


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
