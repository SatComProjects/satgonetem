"""Contract tests for the shared Bird routing lifecycle."""

from __future__ import annotations

import pytest

from satgonetem.routing.bird_daemon import BirdRoutingDaemon
from tests.support import DummyTopology

pytestmark = pytest.mark.unit


class _DummyBird(BirdRoutingDaemon):
    protocol_name = "DummyBird"

    def __init__(self, topology, *, fail=(), valid=True):
        super().__init__(topology)
        self.fail = set(fail)
        self.valid = valid
        self.initialized = []
        self.stopped = []
        self.validation_calls = []

    def _validate_nodes(self, nodes):
        self.validation_calls.append(list(nodes))
        return self.valid

    def _init_node(self, node):
        if node.name in self.fail:
            raise RuntimeError(f"{node.name} boom")
        self.initialized.append(node.name)

    def _stop_node(self, node):
        self.stopped.append(node.name)


def _topology() -> DummyTopology:
    topology = DummyTopology(status=True)
    topology.add_satellite("Sat0")
    topology.add_ground_station("Gnd0")
    return topology


def test_init_runs_every_node_and_reports_success():
    topology = _topology()
    daemon = _DummyBird(topology)

    assert daemon.init(max_workers=2) is True
    assert sorted(daemon.initialized) == ["Gnd0", "Sat0"]
    assert daemon.validation_calls


def test_init_returns_false_when_validation_fails():
    topology = _topology()
    daemon = _DummyBird(topology, valid=False)

    assert daemon.init() is False
    assert daemon.initialized == []


def test_init_returns_false_when_a_node_fails():
    topology = _topology()
    daemon = _DummyBird(topology, fail={"Sat0"})

    assert daemon.init() is False
    assert daemon.initialized == ["Gnd0"]


def test_update_requires_active_topology():
    topology = _topology()
    topology.status = False
    daemon = _DummyBird(topology)

    daemon.update([], max_workers=1)

    assert daemon.initialized == []


def test_update_reinitializes_affected_nodes_once():
    topology = _topology()
    daemon = _DummyBird(topology)
    sat = topology.satellites[0]
    gnd = topology.ground_stations[0]
    link1 = type("Link", (), {"source": sat, "target": gnd})()
    link2 = type("Link", (), {"source": sat, "target": gnd})()

    daemon.update([link1, link2], max_workers=1)

    assert sorted(daemon.initialized) == ["Gnd0", "Sat0"]


def test_update_logs_and_continues_after_node_failure(caplog):
    import logging

    topology = _topology()
    daemon = _DummyBird(topology, fail={"Sat0"})
    sat = topology.satellites[0]
    gnd = topology.ground_stations[0]
    link = type("Link", (), {"source": sat, "target": gnd})()

    with caplog.at_level(logging.ERROR):
        daemon.update([link])

    assert "DummyBird update failed on Sat0" in caplog.text
    assert daemon.initialized == ["Gnd0"]


def test_remove_single_node_stops_only_that_node():
    topology = _topology()
    daemon = _DummyBird(topology)
    node = topology.satellites[0]

    daemon.remove(node=node)

    assert daemon.stopped == ["Sat0"]


def test_remove_all_nodes_stops_every_node():
    topology = _topology()
    daemon = _DummyBird(topology)

    daemon.remove(max_workers=2)

    assert sorted(daemon.stopped) == ["Gnd0", "Sat0"]
