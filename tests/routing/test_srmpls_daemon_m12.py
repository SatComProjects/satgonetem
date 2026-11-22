"""Regression tests for SR-MPLS apply worker sizing (audit M12).

``_apply_sr_node_sids_to_satellites``, ``_apply_sr_routes_to_ground_stations``
and ``_apply_sr_routes_to_satellites`` sized their thread pool with
``min(32, cpu * 4, len(nodes))``.  When a node class is empty the expression is
zero and ``ThreadPoolExecutor`` raises ``ValueError``; ``init()`` then swallows
the error and returns ``False``.  An empty or single-class topology must be a
successful no-op instead.
"""

from __future__ import annotations

import networkx as nx
import pytest

from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from tests.support import DummyTopology

pytestmark = pytest.mark.unit


def _topology(satellites: int, ground_stations: int) -> DummyTopology:
    topology = DummyTopology(gonetem_is_on=True)
    for index in range(satellites):
        topology.add_satellite(f"Sat{index}")
    for index in range(ground_stations):
        topology.add_ground_station(f"Gnd{index}")
    topology.get_current_graph = lambda: nx.Graph()
    return topology


@pytest.mark.parametrize(
    ("satellites", "ground_stations"),
    [
        pytest.param(0, 0, id="empty"),
        pytest.param(1, 0, id="satellite-only"),
        pytest.param(0, 1, id="ground-station-only"),
    ],
)
def test_init_with_empty_node_class_is_a_successful_noop(
    satellites: int, ground_stations: int
) -> None:
    daemon = SRMPLSDaemon(_topology(satellites, ground_stations))

    assert daemon.init() is True
    assert daemon.is_initialized


@pytest.mark.parametrize(
    ("satellites", "ground_stations"),
    [
        pytest.param(0, 0, id="empty"),
        pytest.param(1, 0, id="satellite-only"),
        pytest.param(0, 1, id="ground-station-only"),
    ],
)
def test_update_with_empty_node_class_does_not_raise(
    satellites: int, ground_stations: int
) -> None:
    daemon = SRMPLSDaemon(_topology(satellites, ground_stations))
    daemon._configure_mpls()
    daemon._init_sr_mpls()

    assert daemon.update([]) is None
