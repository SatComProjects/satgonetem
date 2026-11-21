"""Tests for SR-MPLS custom-path and statistics APIs."""

from __future__ import annotations

import pytest

from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.srmpls_label_manager import SegmentRoutingManager
from tests.support import DummyTopology

pytestmark = pytest.mark.unit


def _daemon() -> SRMPLSDaemon:
    topology = DummyTopology(status=False)
    topology.add_satellite("Sat1")
    topology.add_ground_station("Gnd2")
    topology.add_ground_station("Gnd0")
    daemon = SRMPLSDaemon(topology)
    daemon._label_manager = SegmentRoutingManager()
    for node in (
        list(topology.satellites.values())
        + list(topology.ground_stations.values())
    ):
        daemon._label_manager.allocate_node_sid(node.id, node.name)
    return daemon


def test_uninitialized_apis_are_safe_or_raise():
    daemon = SRMPLSDaemon(DummyTopology())

    with pytest.raises(RuntimeError, match="not initialized"):
        daemon.set_sr_custom_path(0, 1, [0, 1])
    with pytest.raises(RuntimeError, match="not initialized"):
        daemon.clear_sr_custom_path(0, 1)

    assert daemon.get_sr_statistics() == {"enabled": False}
    assert daemon.list_sr_custom_paths() == []
    assert daemon._apply_sr_mpls_routes() == 0


@pytest.mark.parametrize(
    "path, error",
    [
        ([0], "at least 2"),
        ([1, 2], "start with source"),
        ([0, 1], "end with destination"),
        ([0, 99, 2], "not found in topology"),
    ],
)
def test_set_custom_path_validates_input(path, error):
    daemon = _daemon()

    with pytest.raises(ValueError, match=error):
        daemon.set_sr_custom_path(0, 2, path)


def test_set_clear_and_list_custom_path():
    daemon = _daemon()

    result = daemon.set_sr_custom_path(0, 2, [0, 1, 2])

    assert result["status"] == "success"
    assert result["path"] == [0, 1, 2]
    assert result["label_stack"] == [
        daemon._label_manager.get_node_sid(1),
        daemon._label_manager.get_node_sid(2),
    ]
    assert result["hop_count"] == 2

    listed = daemon.list_sr_custom_paths()
    assert listed == [
        {
            "source_id": 0,
            "source_name": "Gnd0",
            "destination_id": 2,
            "destination_name": "Gnd2",
            "path": [0, 1, 2],
            "label_stack": [
                daemon._label_manager.get_node_sid(1),
                daemon._label_manager.get_node_sid(2),
            ],
            "hop_count": 2,
        }
    ]

    cleared = daemon.clear_sr_custom_path(0, 2)
    assert cleared["reverted_to_shortest"] is True
    assert "removed" in cleared["message"]
    assert daemon.list_sr_custom_paths() == []

    missing = daemon.clear_sr_custom_path(0, 2)
    assert missing["reverted_to_shortest"] is False


def test_statistics_report_enabled_routes():
    daemon = _daemon()
    daemon.sr_lfib = {0: [object(), object()], 1: [object()]}

    stats = daemon.get_sr_statistics()

    assert stats["enabled"] is True
    assert stats["sr_routes"] == 3
    assert stats["ground_stations_with_routes"] == 2
    assert stats["node_sids_allocated"] == 3
