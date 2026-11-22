"""Tests for the TopologyManager connectivity convenience methods."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from satgonetem.services.topology_satcom import TopologyManager


class _Matrix:
    def __init__(self):
        self.printed = False

    def print_summary(self):
        self.printed = True

    def to_dict(self):
        return {
            "summary": {"pairs": 0, "reachable": 0, "unreachable": 0},
            "results": [],
        }


def _manager(nodes):
    manager = TopologyManager.__new__(TopologyManager)
    manager.satellites = {
        node.id: node for node in nodes if node.name.startswith("Sat")
    }
    manager.ground_stations = {
        node.id: node for node in nodes if node.name.startswith("Gnd")
    }
    return manager


def test_topology_manager_exposes_backend_controls():
    for name in (
        "restart_node",
        "stop_all_nodes",
        "start_all_nodes",
        "set_link_state",
        "set_link_impairment",
        "capture_interface",
        "get_backend_status",
        "save_snapshot",
        "connectivity_matrix",
    ):
        assert callable(getattr(TopologyManager, name))


def test_connectivity_matrix_bounds_pairs():
    nodes = [SimpleNamespace(name=f"Gnd{i}", id=i) for i in range(3)]
    manager = _manager(nodes)

    with pytest.raises(ValueError, match="max_pairs"):
        manager.connectivity_matrix(nodes=nodes, max_pairs=4)


def test_connectivity_matrix_delegates_to_builder():
    nodes = [SimpleNamespace(name="Gnd0", id=0), SimpleNamespace(name="Gnd1", id=1)]
    manager = _manager(nodes)
    expected = _Matrix()

    with patch(
        "satgonetem.connectivity.build_connectivity_matrix", return_value=expected
    ) as build:
        matrix = manager.connectivity_matrix(nodes=nodes, max_workers=3)

    assert matrix is expected
    build.assert_called_once()
    assert build.call_args.kwargs["max_workers"] == 3


def test_test_connectivity_returns_dict_and_prints():
    manager = _manager([])
    matrix = _Matrix()
    with patch(
        "satgonetem.connectivity.build_connectivity_matrix", return_value=matrix
    ):
        result = manager.test_connectivity()

    assert matrix.printed is True
    assert result["summary"]["pairs"] == 0
