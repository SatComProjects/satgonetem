"""Regression tests for M05: stop_gonetem teardown robustness.

The stop path must isolate per-flow failures, retain flows that are still
running, keep the launcher available for a retry when closing fails, clear
project handles only after a successful teardown, and remove the routing
daemon before clearing its state.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin
from satgonetem.utils.constants import MAX_WORKERS


class DummyTopology(NetworkLifecycleMixin):
    def __init__(self):
        self.status = False
        self.gonetem_status = False
        self.routing_initiated = False
        self.routing_method = "static"
        self.routing_daemon = None
        self.launcher = None
        self.running = False
        self._sim_thread = None
        self._active_flows = []
        self.hil_manager = None
        self.project_id = None
        self.client = None
        self.request = None

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


def make_flow(state="RUNNING", alive_after_join=(True, False)):
    flow = MagicMock()
    flow.status.return_value = SimpleNamespace(name=state)
    thread = MagicMock()
    thread.is_alive.side_effect = list(alive_after_join)
    flow._thread = thread
    return flow


def test_stop_isolates_flow_status_errors_and_still_closes_project(caplog):
    topology = DummyTopology()
    broken = MagicMock()
    broken.status.side_effect = RuntimeError("status probe failed")
    broken._thread = None
    healthy = make_flow()
    topology._active_flows = [broken, healthy]
    launcher = MagicMock()
    topology.launcher = launcher

    elapsed = topology.stop_gonetem()

    assert elapsed >= 0
    healthy.stop.assert_called_once_with()
    launcher.close_project.assert_called_once_with()
    assert topology._active_flows == []
    assert "status probe failed" in caplog.text


def test_stop_retains_flows_whose_threads_ignore_the_stop_timeout(caplog):
    topology = DummyTopology()
    stuck = make_flow(alive_after_join=(True, True))
    finished = make_flow(state="STOPPED", alive_after_join=(False,))
    topology._active_flows = [stuck, finished]

    topology.stop_gonetem()

    stuck.stop.assert_called_once_with()
    stuck._thread.join.assert_called_once()
    finished.stop.assert_not_called()
    assert topology._active_flows == [stuck]
    assert "did not stop" in caplog.text


def test_stop_clears_project_handles_after_successful_teardown():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.project_id = "project-1"
    topology.client = MagicMock()
    topology.request = MagicMock()
    topology.launcher = MagicMock()

    topology.stop_gonetem()

    assert topology.project_id is None
    assert topology.client is None
    assert topology.request is None


def test_stop_clears_project_handles_even_without_a_launcher():
    topology = DummyTopology()
    topology.project_id = "project-1"
    topology.client = MagicMock()
    topology.request = MagicMock()

    topology.stop_gonetem()

    assert topology.project_id is None
    assert topology.client is None
    assert topology.request is None


class FailingLauncher:
    def __init__(self, force_close_error=None, force_close_result=True):
        self.force_close_error = force_close_error
        self.force_close_result = force_close_result
        self.force_close_calls = 0

    def close_project(self):
        raise RuntimeError("ProjectClose failed")

    def force_close_project(self):
        self.force_close_calls += 1
        if self.force_close_error is not None:
            raise self.force_close_error
        return self.force_close_result


class ClientClearingLauncher:
    """Mimics GoNetEmLauncher: close() always drops the client in finally."""

    def __init__(self, force_close_result):
        self._client = MagicMock()
        self.force_close_result = force_close_result
        self.force_close_calls = 0

    def close_project(self):
        try:
            raise RuntimeError("ProjectClose failed")
        finally:
            self._client = None

    def force_close_project(self):
        self.force_close_calls += 1
        return self.force_close_result


def test_stop_uses_force_close_fallback_and_clears_launcher():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.project_id = "project-1"
    launcher = FailingLauncher()
    topology.launcher = launcher

    topology.stop_gonetem()

    assert launcher.force_close_calls == 1
    assert topology.launcher is None
    assert topology.project_id is None


def test_stop_retains_handles_when_force_close_reports_failure():
    """A fallback that reports failure must not be mistaken for success."""
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.project_id = "project-1"
    topology.client = MagicMock()
    topology.request = MagicMock()
    launcher = ClientClearingLauncher(force_close_result=False)
    topology.launcher = launcher

    with pytest.raises(RuntimeError, match="ProjectClose failed"):
        topology.stop_gonetem()

    assert launcher.force_close_calls == 1
    assert launcher._client is None
    assert topology.launcher is launcher
    assert topology.project_id == "project-1"
    assert topology.client is not None
    assert topology.request is not None
    assert topology.gonetem_status is False
    assert topology.routing_initiated is False


def test_stop_retains_launcher_and_handles_when_force_close_also_fails():
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.project_id = "project-1"
    topology.client = MagicMock()
    topology.request = MagicMock()
    launcher = FailingLauncher(force_close_error=RuntimeError("ForceClose failed"))
    topology.launcher = launcher

    with pytest.raises(RuntimeError, match="ProjectClose failed"):
        topology.stop_gonetem()

    assert launcher.force_close_calls == 1
    assert topology.launcher is launcher
    assert topology.project_id == "project-1"
    assert topology.client is not None
    assert topology.request is not None
    assert topology.gonetem_status is False
    assert topology.routing_initiated is False


def test_stop_removes_routing_daemon_before_clearing_it():
    topology = DummyTopology()
    topology.routing_initiated = True
    daemon = MagicMock()
    topology.routing_daemon = daemon
    topology.routing_method = "static"
    topology.launcher = MagicMock()

    topology.stop_gonetem()

    daemon.remove.assert_called_once_with(max_workers=MAX_WORKERS)
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
    assert topology.routing_method is None


def test_stop_logs_routing_daemon_removal_failure_and_continues(caplog):
    topology = DummyTopology()
    topology.routing_initiated = True
    daemon = MagicMock()
    daemon.remove.side_effect = RuntimeError("route removal failed")
    topology.routing_daemon = daemon
    launcher = MagicMock()
    topology.launcher = launcher

    elapsed = topology.stop_gonetem()

    assert elapsed >= 0
    daemon.remove.assert_called_once_with(max_workers=MAX_WORKERS)
    launcher.close_project.assert_called_once_with()
    assert topology.routing_daemon is None
    assert topology.routing_initiated is False
    assert "route removal failed" in caplog.text


def test_stop_is_idempotent_after_a_successful_teardown():
    topology = DummyTopology()
    launcher = MagicMock()
    topology.launcher = launcher

    first = topology.stop_gonetem()
    second = topology.stop_gonetem()

    assert first >= 0
    assert second >= 0
    launcher.close_project.assert_called_once_with()
