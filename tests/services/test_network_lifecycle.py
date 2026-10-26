"""Tests for HIL-aware GoNetEm startup filtering."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def get_gonetem_status(self):
        return self.gonetem_status

    def set_status(self, value):
        self.status = value

    def set_gonetem_status(self, value):
        self.gonetem_status = value


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
