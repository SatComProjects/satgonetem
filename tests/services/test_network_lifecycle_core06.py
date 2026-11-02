"""Regression tests for CORE-06: failed startup must not leak a project."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin


LAUNCHER_PATH = "satgonetem.launchers.gonetem_launcher.GoNetEmLauncher"


class DummyTopology(NetworkLifecycleMixin):
    def __init__(self):
        self.gonetem_server = "localhost:10110"
        self.project_name = "test"
        self.isl_link_capacity = 100_000
        self.gnd_link_capacity = 100_000
        self.ground_object_link_capacity = 100_000
        self.status = False
        self.gonetem_status = False
        self.routing_initiated = False
        self.launcher = None
        self.running = False
        self._sim_thread = None
        self._active_flows = []
        self.hil_manager = None
        self.routing_method = "static"
        self.start_time_ = None
        self.satellites = {}
        self.ground_stations = {}
        self.links = {}

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


def make_launcher():
    launcher = MagicMock()
    launcher.start_containers.return_value = (1.0, 2.0)
    launcher.force_close_project.return_value = None
    return launcher


def add_hil_link(topology):
    sat = SimpleNamespace(name="Sat0")
    gnd = SimpleNamespace(name="Gnd0")
    topology.satellites = {sat.name: sat}
    topology.ground_stations = {gnd.name: gnd}
    topology.links = {"lnk": SimpleNamespace(source=sat, target=gnd, is_active=True)}
    return topology.links["lnk"]


def assert_clean_failure(topology):
    assert topology.launcher is None
    assert topology.status is False
    assert topology.gonetem_status is False
    assert topology.routing_initiated is False
    assert topology.start_time_ is None


def test_start_containers_failure_closes_project_and_resets_state():
    topology = DummyTopology()
    launcher = make_launcher()
    launcher.start_containers.side_effect = RuntimeError("container start failed")

    with patch(LAUNCHER_PATH, return_value=launcher):
        with pytest.raises(RuntimeError, match="container start failed"):
            topology.start_gonetem()

    launcher.force_close_project.assert_called_once_with()
    assert_clean_failure(topology)


@pytest.mark.parametrize("failing_step", ["wire_links", "hil_wire_links"])
def test_wiring_failure_closes_project_and_resets_state(failing_step):
    topology = DummyTopology()
    launcher = make_launcher()

    if failing_step == "hil_wire_links":
        add_hil_link(topology)
        topology.hil_manager = MagicMock()
        topology.hil_manager.is_hil_node.return_value = False
        topology.hil_manager.is_hil_link.return_value = True
        topology.hil_manager.wire_links.side_effect = RuntimeError("HIL wiring failed")
        expected_error = "HIL wiring failed"
    else:
        launcher.wire_links.side_effect = RuntimeError("link wiring failed")
        expected_error = "link wiring failed"

    with patch(LAUNCHER_PATH, return_value=launcher):
        with pytest.raises(RuntimeError, match=expected_error):
            topology.start_gonetem()

    launcher.force_close_project.assert_called_once_with()
    assert_clean_failure(topology)


def test_retry_after_failure_opens_one_fresh_launcher():
    topology = DummyTopology()
    failed_launcher = make_launcher()
    failed_launcher.start_containers.side_effect = RuntimeError("startup failed")
    fresh_launcher = make_launcher()

    with patch(
        LAUNCHER_PATH, side_effect=[failed_launcher, fresh_launcher]
    ) as launcher_cls:
        with pytest.raises(RuntimeError, match="startup failed"):
            topology.start_gonetem()

        assert topology.launcher is None
        assert launcher_cls.call_count == 1

        topology.start_gonetem()

    assert launcher_cls.call_count == 2
    failed_launcher.force_close_project.assert_called_once_with()
    fresh_launcher.force_close_project.assert_not_called()
    assert topology.launcher is fresh_launcher
    assert topology.status is True
    assert topology.gonetem_status is True
    assert topology.start_time_ is not None

    live_launchers = [
        launcher
        for launcher in (failed_launcher, fresh_launcher)
        if not launcher.force_close_project.called
    ]
    assert live_launchers == [fresh_launcher]


def test_force_close_failure_does_not_mask_startup_error():
    topology = DummyTopology()
    launcher = make_launcher()
    launcher.start_containers.side_effect = RuntimeError("container start failed")
    launcher.force_close_project.side_effect = RuntimeError("cleanup failed")

    with patch(LAUNCHER_PATH, return_value=launcher):
        with pytest.raises(RuntimeError, match="container start failed"):
            topology.start_gonetem()

    launcher.force_close_project.assert_called_once_with()
    assert_clean_failure(topology)
