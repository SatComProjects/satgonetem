"""Regression tests for CORE-05: force stop must stop loop and flows."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from satgonetem.services.mixins.network_lifecycle import (
    _FLOW_STOP_TIMEOUT_SEC,
    NetworkLifecycleMixin,
)


class DummyTopology(NetworkLifecycleMixin):
    def __init__(self):
        self.status = False
        self.gonetem_status = False
        self.routing_initiated = False
        self.routing_method = "static"
        self.launcher = None
        self.running = False
        self._sim_thread = None
        self._active_flows = []
        self.hil_manager = None
        self.stop = MagicMock()

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


def make_flow(state="RUNNING", thread_alive=True):
    flow = MagicMock()
    flow.status.return_value = SimpleNamespace(name=state)
    thread = MagicMock()
    thread.is_alive.return_value = thread_alive
    flow._thread = thread
    return flow


def force_stop(topology):
    with pytest.warns(ResourceWarning, match="does not clean up properly"):
        return topology.force_stop_gonetem()


def assert_state_reset(topology):
    assert topology.launcher is None
    assert topology.status is False
    assert topology.gonetem_status is False
    assert topology.routing_initiated is False
    assert topology.routing_method is None


@pytest.mark.parametrize("running,thread_alive", [(True, False), (False, True)])
def test_force_stop_stops_simulation_loop(running, thread_alive):
    topology = DummyTopology()
    topology.running = running
    if thread_alive:
        thread = MagicMock()
        thread.is_alive.return_value = True
        topology._sim_thread = thread

    force_stop(topology)

    topology.stop.assert_called_once_with()


def test_force_stop_does_not_stop_idle_loop():
    topology = DummyTopology()
    thread = MagicMock()
    thread.is_alive.return_value = False
    topology._sim_thread = thread

    force_stop(topology)

    topology.stop.assert_not_called()


def test_force_stop_stops_and_joins_active_flows():
    topology = DummyTopology()
    running = make_flow("RUNNING")
    pending = make_flow("PENDING")
    finished = make_flow("STOPPED", thread_alive=False)
    topology._active_flows = [running, pending, finished]

    force_stop(topology)

    running.stop.assert_called_once_with()
    pending.stop.assert_called_once_with()
    finished.stop.assert_not_called()
    for flow in (running, pending):
        flow._thread.join.assert_called_once_with(timeout=_FLOW_STOP_TIMEOUT_SEC)
    assert topology._active_flows == []


def test_force_stop_uses_force_close_and_resets_state():
    topology = DummyTopology()
    topology.status = True
    topology.gonetem_status = True
    topology.routing_initiated = True
    launcher = MagicMock()
    topology.launcher = launcher

    force_stop(topology)

    launcher.force_close_project.assert_called_once_with()
    launcher.close_project.assert_not_called()
    assert_state_reset(topology)


def test_force_stop_tears_down_hil_before_force_close():
    topology = DummyTopology()
    hil_manager = MagicMock()
    topology.hil_manager = hil_manager
    launcher = MagicMock()
    topology.launcher = launcher

    force_stop(topology)

    hil_manager.teardown_all.assert_called_once_with()
    launcher.force_close_project.assert_called_once_with()


def test_force_stop_resets_state_when_force_close_raises():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.routing_initiated = True
    launcher = MagicMock()
    launcher.force_close_project.side_effect = RuntimeError("ProjectForceClose failed")
    topology.launcher = launcher

    force_stop(topology)

    launcher.force_close_project.assert_called_once_with()
    assert_state_reset(topology)


def test_force_stop_resets_state_when_hil_teardown_raises():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.routing_initiated = True
    topology.hil_manager = MagicMock()
    topology.hil_manager.teardown_all.side_effect = RuntimeError("HIL teardown failed")
    launcher = MagicMock()
    topology.launcher = launcher

    with pytest.warns(ResourceWarning, match="does not clean up properly"):
        with pytest.raises(RuntimeError, match="HIL teardown failed"):
            topology.force_stop_gonetem()

    launcher.force_close_project.assert_called_once_with()
    assert_state_reset(topology)
