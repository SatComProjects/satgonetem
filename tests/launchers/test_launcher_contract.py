"""Contract tests for NetworkLauncher and backend selection."""

from __future__ import annotations

import pytest

from satgonetem.launchers.base_launcher import NetworkLauncher
from satgonetem.launchers.registry import create_launcher, register_launcher
from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin

pytestmark = pytest.mark.unit


class FakeLauncher(NetworkLauncher):
    instances = []

    def __init__(self, **kwargs):
        super().__init__(
            kwargs["project_name"],
            kwargs.get("isl_capacity_kbps", 100_000),
            kwargs.get("gnd_capacity_kbps", 100_000),
            kwargs.get("ground_object_capacity_kbps", 100_000),
            kwargs.get("netem_limit", 1000),
            kwargs.get("txqueuelen", 1000),
        )
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        FakeLauncher.instances.append(self)

    def start_containers(
        self,
        nodes,
        workers=64,
        progress_cb=None,
        controller=False,
        links=None,
    ):
        self.started = True
        return (0.1, 0.2)

    def wire_links(self, links, workers=64, progress_cb=None):
        pass

    def close_project(self, progress_cb=None):
        self.closed = True

    def force_close_project(self):
        self.closed = True
        return True

    def update_link(self, link):
        pass

    def add_link(self, link):
        pass

    def delete_link(self, link):
        pass

    def set_link_capacities(self, isl_kbps, gnd_kbps, links):
        pass


class DummyTopology(NetworkLifecycleMixin):
    def __init__(self, launcher_name="FAKE"):
        self.network_launcher = launcher_name
        self.gonetem_server = "localhost:10110"
        self.project_name = "test"
        self.isl_link_capacity = 100_000
        self.gnd_link_capacity = 100_000
        self.ground_object_link_capacity = 100_000
        self.link_netem_limit = 1000
        self.link_txqueuelen = 1000
        self.satellites = {}
        self.ground_stations = {}
        self.links = {}
        self.hil_manager = None
        self.status = False
        self.gonetem_status = False
        self.routing_initiated = False

    def get_satellites(self):
        return []

    def get_ground_stations(self):
        return []

    def get_gonetem_status(self):
        return self.gonetem_status

    def set_status(self, value):
        self.status = value

    def set_gonetem_status(self, value):
        self.gonetem_status = value

    def set_routing_initiated(self, value):
        self.routing_initiated = value


def test_launcher_contract_accepts_a_minimal_backend():
    register_launcher("FAKE", FakeLauncher)

    launcher = create_launcher(
        "FAKE",
        topology_manager=object(),
        server_address="unused",
        project_name="p",
    )

    assert isinstance(launcher, NetworkLauncher)
    assert launcher.start_containers([], links=[]) == (0.1, 0.2)
    assert launcher.force_close_project() is True
    assert launcher.closed is True


def test_network_launcher_selects_registered_backend():
    register_launcher("FAKE", FakeLauncher)
    topology = DummyTopology("FAKE")

    result = topology.start_gonetem()

    assert result == (0.1, 0.2)
    assert FakeLauncher.instances[-1].started is True
    assert topology.launcher is FakeLauncher.instances[-1]


def test_network_launcher_rejects_unknown_backend():
    with pytest.raises(ValueError, match="Unknown network launcher"):
        create_launcher("missing", project_name="p")
