"""Direct contract tests for the SR-MPLS route installer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.srmpls_installer import SRMPLSRouteInstaller

pytestmark = pytest.mark.unit


class _FakeTopology:
    def __init__(self, satellites=(), ground_stations=()) -> None:
        self.satellites = {node.id: node for node in satellites}
        self.ground_stations = {node.id: node for node in ground_stations}

    def _get_interface_to_peer(self, node, peer_id):
        for iface in node.interfaces:
            try:
                if int(iface.name.split(".")[1]) == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None


class _FakeLabelManager:
    def __init__(self, sids: dict[int, int]) -> None:
        self._sids = sids

    def get_node_sid(self, node_id: int) -> int | None:
        return self._sids.get(node_id)


class _RecordingGroundStation(GroundStation):
    def __init__(self, node_id: int, name: str, loopback: str) -> None:
        super().__init__(name)
        self.id = node_id
        self.loopback.set_ip(loopback)
        self.container = object()
        self.commands: list[list[str]] = []

    def execute_command(self, command: list[str]) -> str:
        self.commands.append(command)
        return ""


class _RecordingSatellite(Satellite):
    def __init__(self, node_id: int, name: str, loopback: str) -> None:
        super().__init__(name)
        self.id = node_id
        self.loopback.set_ip(loopback)
        self.set_addressable(True)
        self.container = object()
        self.commands: list[list[str]] = []

    def execute_command(self, command: list[str]) -> str:
        self.commands.append(command)
        return ""


def _connect(left, right, left_ip: str, right_ip: str) -> None:
    left_iface = Interface(f"{left.name}.{right.id}")
    right_iface = Interface(f"{right.name}.{left.id}")
    left_iface.peer = right_iface
    right_iface.peer = left_iface
    left_iface.set_ip(left_ip)
    right_iface.set_ip(right_ip)
    left.interfaces.append(left_iface)
    right.interfaces.append(right_iface)


def _stack_entry(command: str) -> SimpleNamespace:
    return SimpleNamespace(to_iproute2_command=lambda: command)


def test_sid_pass_installs_own_sid_and_neighbour_forward_only() -> None:
    sat = _RecordingSatellite(1, "Sat1", "10.0.0.1")
    gs = _RecordingGroundStation(2, "Gnd2", "10.0.0.2")
    _connect(sat, gs, "10.10.0.1", "10.10.0.2")
    topology = _FakeTopology([sat], [gs])
    manager = _FakeLabelManager({1: 16001, 2: 16002})
    stack_route = (
        "ip route replace 10.0.0.2/32 encap mpls 16002 via 10.10.0.2 dev Sat1.2"
    )
    prev_sid: dict[int, list[str]] = {}
    prev_stack: dict[int, list[str]] = {1: [stack_route]}

    errors = SRMPLSRouteInstaller(topology).apply_sr_node_sids_to_satellites(
        manager, {1: [_stack_entry(stack_route)]}, prev_sid, prev_stack
    )

    iface_name = sat.interfaces[0].get_iname()
    own = "ip -f mpls route replace 16001 dev lo"
    forward = f"ip -f mpls route replace 16002 via inet 10.10.0.2 dev {iface_name}"
    assert errors == 0
    assert sat.commands == [["sh", "-lc", f"set -e; {own}; {forward}"]]
    assert prev_sid[1] == [own, forward]

    # A second identical pass must not touch the node again.
    assert (
        SRMPLSRouteInstaller(topology).apply_sr_node_sids_to_satellites(
            manager, {1: [_stack_entry(stack_route)]}, prev_sid, prev_stack
        )
        == 0
    )
    assert len(sat.commands) == 1


def test_ground_station_pass_uses_full_string_set_diff() -> None:
    gs = _RecordingGroundStation(2, "Gnd2", "10.0.0.2")
    topology = _FakeTopology((), [gs])
    manager = _FakeLabelManager({2: 16002})
    previous = "ip route replace 10.0.0.9/32 via 10.0.0.1 dev Gnd2.1"
    desired = "ip route replace 10.0.0.9/32 via 10.0.0.3 dev Gnd2.3"
    prev: dict[int, list[str]] = {2: [previous]}

    errors = SRMPLSRouteInstaller(topology).apply_sr_routes_to_ground_stations(
        manager, {2: [_stack_entry(desired)]}, prev
    )

    assert errors == 0
    # The full-string diff deletes the old spelling even though the route
    # identity (prefix) is unchanged.
    assert gs.commands == [
        [
            "sh",
            "-lc",
            "set -e; if ip route show exact 10.0.0.9/32 2>&1 | grep -q .; "
            "then ip route del 10.0.0.9/32; fi; "
            "ip -f mpls route replace 16002 dev lo; " + desired,
        ]
    ]
    assert prev[2] == ["ip -f mpls route replace 16002 dev lo", desired]


def test_install_source_routes_reinstalls_every_desired_command() -> None:
    gs = _RecordingGroundStation(2, "Gnd2", "10.0.0.2")
    topology = _FakeTopology((), [gs])
    manager = _FakeLabelManager({2: 16002})
    desired = "ip route replace 10.0.0.9/32 via 10.0.0.3 dev Gnd2.3"
    commands = ["ip -f mpls route replace 16002 dev lo", desired]
    prev: dict[int, list[str]] = {2: list(commands)}

    SRMPLSRouteInstaller(topology).install_source_routes(
        gs, manager, [_stack_entry(desired)], prev
    )

    assert gs.commands == [["sh", "-lc", "set -e; " + "; ".join(commands)]]
    assert prev[2] == commands


def test_enable_pass_uses_the_supplied_callable_and_counts_failures() -> None:
    sat = MagicMock()
    sat.name = "Sat0"
    sat.container = object()
    gs = MagicMock()
    gs.name = "Gnd1"
    gs.container = object()
    topology = _FakeTopology([sat], [gs])

    def enable(node) -> None:
        if node.name == "Gnd1":
            raise RuntimeError("container error")

    errors = SRMPLSRouteInstaller(topology).enable_mpls_on_all_nodes(1, enable)

    assert errors == 1
