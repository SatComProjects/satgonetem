"""Regression tests for retrying a retained project close.

A failed stop retains the launcher and project handles, but the graceful
``close_project()`` may already have cleared the gRPC client. The next
``stop_gonetem()`` therefore has to retry the forced close instead of calling
the graceful close again, and it may only drop the handles once the forced
close reports success.
"""

from unittest.mock import MagicMock

import pytest

from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin


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


class RetryableLauncher:
    """Launcher whose forced close outcome is scripted per attempt."""

    def __init__(self, force_close_results):
        self.force_close_results = list(force_close_results)
        self.close_project_calls = 0
        self.force_close_calls = 0

    def close_project(self):
        self.close_project_calls += 1
        raise RuntimeError("ProjectClose failed")

    def force_close_project(self):
        self.force_close_calls += 1
        return self.force_close_results.pop(0)


def make_retained_topology(force_close_results):
    topology = DummyTopology()
    topology.gonetem_status = True
    topology.project_id = "project-1"
    topology.client = MagicMock()
    topology.request = MagicMock()
    launcher = RetryableLauncher(force_close_results)
    topology.launcher = launcher
    return topology, launcher


def test_second_stop_retries_force_close_and_clears_handles():
    topology, launcher = make_retained_topology([False, True])

    with pytest.raises(RuntimeError, match="ProjectClose failed"):
        topology.stop_gonetem()

    assert launcher.force_close_calls == 1
    assert topology.launcher is launcher
    assert topology.project_id == "project-1"
    assert topology.client is not None
    assert topology.request is not None
    assert topology._pending_force_close is True

    topology.stop_gonetem()

    assert launcher.force_close_calls == 2
    assert launcher.close_project_calls == 1
    assert topology.launcher is None
    assert topology.project_id is None
    assert topology.client is None
    assert topology.request is None
    assert topology._pending_force_close is False


def test_second_stop_retains_handles_when_retry_force_close_still_fails():
    topology, launcher = make_retained_topology([False, False])

    with pytest.raises(RuntimeError, match="ProjectClose failed"):
        topology.stop_gonetem()

    with pytest.raises(RuntimeError, match="force-close did not confirm"):
        topology.stop_gonetem()

    assert launcher.force_close_calls == 2
    assert launcher.close_project_calls == 1
    assert topology.launcher is launcher
    assert topology.project_id == "project-1"
    assert topology.client is not None
    assert topology.request is not None
    assert topology._pending_force_close is True


def test_normal_close_never_sets_the_pending_force_close_flag():
    topology = DummyTopology()
    launcher = MagicMock()
    topology.launcher = launcher

    topology.stop_gonetem()

    assert launcher.close_project.call_count == 1
    assert launcher.force_close_project.call_count == 0
    assert topology._pending_force_close is False
