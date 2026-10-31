"""Tests for HIL-aware GoNetEm startup filtering."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin
from satgonetem.utils.constants import MAX_WORKERS


class DummyTopology(NetworkLifecycleMixin):
    def __init__(self):
        self.gonetem_server = "localhost:10110"
        self.project_name = "test"
        self.isl_link_capacity = 100_000
        self.gnd_link_capacity = 100_000
        self.ground_object_link_capacity = 100_000
        self.status = False
        self.gonetem_status = False
        self.launcher = None
        self.running = False
        self._sim_thread = None
        self._active_flows = []
        self.hil_manager = None
        self.routing_method = "static"

    def get_gonetem_status(self):
        return self.gonetem_status

    def get_running(self):
        return self.running

    def set_status(self, value):
        self.status = value

    def set_gonetem_status(self, value):
        self.gonetem_status = value

    def set_routing_initiated(self, value):
        self.routing_initiated = value


def test_start_gonetem_passes_only_non_hil_nodes_and_active_links():
    topology = DummyTopology()
    sat = SimpleNamespace(name="Sat0")
    gnd = SimpleNamespace(name="Gnd0")
    hil_gnd = SimpleNamespace(name="Gnd1")
    active_link = SimpleNamespace(source=sat, target=gnd, is_active=True)
    hil_link = SimpleNamespace(source=sat, target=hil_gnd, is_active=True)
    inactive_link = SimpleNamespace(source=sat, target=gnd, is_active=False)

    topology.satellites = {sat.name: sat}
    topology.ground_stations = {gnd.name: gnd, hil_gnd.name: hil_gnd}
    topology.links = {
        "active": active_link,
        "hil": hil_link,
        "inactive": inactive_link,
    }

    hil_manager = MagicMock()
    hil_manager.is_hil_node.side_effect = lambda name: name == hil_gnd.name
    hil_manager.is_hil_link.side_effect = lambda link: hil_gnd.name in (
        link.source.name,
        link.target.name,
    )
    topology.hil_manager = hil_manager

    fake_launcher = MagicMock()
    fake_launcher.start_containers.return_value = (1.0, 2.0)

    with patch(
        "satgonetem.launchers.gonetem_launcher.GoNetEmLauncher",
        return_value=fake_launcher,
    ):
        topology.start_gonetem()

    fake_launcher.start_containers.assert_called_once_with(
        [sat, gnd],
        MAX_WORKERS,
        controller=False,
        links=[active_link],
    )
    fake_launcher.wire_links.assert_called_once_with([active_link], MAX_WORKERS)
    hil_manager.wire_links.assert_called_once_with([hil_link])
    assert topology.launcher is fake_launcher
    assert topology.status is True
    assert topology.gonetem_status is True


def test_stop_gonetem_stops_flows_and_clears_tracking():
    topology = DummyTopology()
    flow = MagicMock()
    flow.status.return_value = SimpleNamespace(name="RUNNING")
    thread = MagicMock()
    thread.is_alive.return_value = True
    flow._thread = thread
    topology._active_flows = [flow]

    topology.stop_gonetem()

    flow.stop.assert_called_once()
    thread.join.assert_called_once()
    assert "timeout" in thread.join.call_args.kwargs
    assert topology._active_flows == []


def test_stop_gonetem_continues_when_flow_stop_fails():
    topology = DummyTopology()
    flow = MagicMock()
    flow.status.return_value = SimpleNamespace(name="PENDING")
    flow.stop.side_effect = RuntimeError("cannot stop")
    flow._thread = None
    topology._active_flows = [flow]

    topology.stop_gonetem()

    assert topology._active_flows == []
    assert topology.gonetem_status is False


def test_stop_gonetem_resets_state_when_close_project_fails():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.routing_initiated = True
    topology.routing_method = "static"

    class FailingLauncher:
        def close_project(self):
            raise RuntimeError("ProjectClose failed")

    topology.launcher = FailingLauncher()

    with pytest.raises(RuntimeError, match="ProjectClose failed"):
        topology.stop_gonetem()

    assert topology.gonetem_status is False
    assert topology.launcher is None
    assert topology.routing_initiated is False
    assert topology.routing_method is None


def test_stop_gonetem_closes_project_when_hil_teardown_fails():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.hil_manager = MagicMock()
    topology.hil_manager.teardown_all.side_effect = RuntimeError("HIL teardown failed")
    launcher = MagicMock()
    topology.launcher = launcher

    with pytest.raises(RuntimeError, match="HIL teardown failed"):
        topology.stop_gonetem()

    launcher.close_project.assert_called_once()
    assert topology.gonetem_status is False
    assert topology.launcher is None
