"""Characterization tests for InterfaceMgrMixin address-plan application."""

from __future__ import annotations

import base64
import ipaddress
from unittest.mock import MagicMock

import pytest

from satgonetem.addressing import Ipv4AddressPlan
from satgonetem.services.mixins.interface_mgr import InterfaceMgrMixin
from tests.support import DummyTopology, FakeCommandExecutor, add_interface

pytestmark = pytest.mark.unit


class _InterfaceMgrHost(InterfaceMgrMixin, DummyTopology):
    """Lightweight topology host exposing InterfaceMgrMixin."""

    def __init__(self, **attributes) -> None:
        defaults = {
            "ground_station_pool": "10.0.0.0/10",
            "satellite_pool": "10.128.0.0/9",
            "user_terminal_pool": "10.64.0.0/10",
            "ground_station_prefix_len": 20,
            "satellite_prefix_len": 27,
            "user_terminal_prefix_len": 20,
            "interfaces": [],
            "address_plan": None,
            "interface_version": 0,
            "status": False,
        }
        defaults.update(attributes)
        super().__init__(**defaults)

    def get_satellites(self):
        return list(self.satellites.values())

    def get_ground_stations(self):
        return list(self.ground_stations.values())

    def get_status(self) -> bool:
        return self.status


@pytest.fixture
def manager() -> _InterfaceMgrHost:
    return _InterfaceMgrHost()


def _pushed_payload(executor: FakeCommandExecutor) -> str:
    """Decode the single base64 ``ip -batch`` payload recorded by *executor*."""
    assert len(executor.calls) == 1
    command, detach = executor.calls[0]
    assert detach is False
    assert command[:2] == ["sh", "-c"]
    shell_command = command[2]
    assert shell_command.startswith("echo ")
    encoded = shell_command.split()[1]
    return base64.b64decode(encoded).decode("utf-8")


def _plan(manager: _InterfaceMgrHost) -> Ipv4AddressPlan:
    plan = manager.address_plan
    assert isinstance(plan, Ipv4AddressPlan)
    return plan


# ----------------------------------------------------------------------
# Address-plan initialization
# ----------------------------------------------------------------------


def test_initialize_address_plan_assigns_blocks_and_loopbacks_in_id_order(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_1 = manager.add_satellite("Sat1")
    ground_5 = manager.add_ground_station("Gnd5")
    satellite_0 = manager.add_satellite("Sat0")
    ground_2 = manager.add_ground_station("Gnd2")

    manager._initialize_address_plan()

    assert isinstance(manager.address_plan, Ipv4AddressPlan)

    assert str(satellite_0.address_block) == "10.128.0.0/27"
    assert satellite_0.loopback.ipv4 == "10.128.0.1"
    assert satellite_0.loopback.prefix_len == 32

    assert str(satellite_1.address_block) == "10.128.0.32/27"
    assert satellite_1.loopback.ipv4 == "10.128.0.33"

    # Registered in sorted-ID order per kind, independent of insertion order.
    assert str(ground_2.address_block) == "10.0.0.0/20"
    assert ground_2.loopback.ipv4 == "10.0.0.1"
    assert str(ground_5.address_block) == "10.0.16.0/20"
    assert ground_5.loopback.ipv4 == "10.0.16.1"


def _manager_with_links(reverse_insertion: bool) -> _InterfaceMgrHost:
    manager = _InterfaceMgrHost()
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    ground_5 = manager.add_ground_station("Gnd5")

    if reverse_insertion:
        manager.add_link(satellite_1, ground_5)
        manager.add_link(satellite_0, satellite_1)
    else:
        manager.add_link(satellite_0, satellite_1)
        manager.add_link(satellite_1, ground_5)

    manager._initialize_address_plan()
    return manager


def test_initialize_address_plan_allocates_links_deterministically() -> None:
    forward = _manager_with_links(reverse_insertion=False)
    reverse = _manager_with_links(reverse_insertion=True)

    satellite_key = frozenset(("Sat0", "Sat1"))
    ground_key = frozenset(("Sat1", "Gnd5"))

    assert _plan(forward).link_address(satellite_key) == _plan(reverse).link_address(
        satellite_key
    )
    assert _plan(forward).link_address(ground_key) == _plan(reverse).link_address(
        ground_key
    )

    satellite_link = _plan(forward).link_address(satellite_key)
    assert satellite_link is not None
    assert satellite_link.prefix_len == 31
    assert (str(satellite_link.source.ip), str(satellite_link.target.ip)) == (
        "10.128.0.4",
        "10.128.0.5",
    )

    ground_link = _plan(forward).link_address(ground_key)
    assert ground_link is not None
    assert ground_link.owner_id == 5
    # The ground station owns the link and receives the lower address even
    # when the link's source is the satellite.
    assert (str(ground_link.source.ip), str(ground_link.target.ip)) == (
        "10.0.0.5",
        "10.0.0.4",
    )


def test_link_address_pair_is_orientation_independent_but_labels_follow_source() -> (
    None
):
    forward = _InterfaceMgrHost()
    satellite_0 = forward.add_satellite("Sat0")
    satellite_1 = forward.add_satellite("Sat1")
    forward_link = forward.add_link(satellite_0, satellite_1)
    forward._initialize_address_plan()

    reverse = _InterfaceMgrHost()
    reverse_satellite_0 = reverse.add_satellite("Sat0")
    reverse_satellite_1 = reverse.add_satellite("Sat1")
    reverse_link = reverse.add_link(reverse_satellite_1, reverse_satellite_0)
    reverse._initialize_address_plan()

    key = frozenset(("Sat0", "Sat1"))
    forward_address = _plan(forward).link_address(key)
    reverse_address = _plan(reverse).link_address(key)
    assert forward_address is not None
    assert reverse_address is not None

    # The /31 pair is the same for both orientations. Current behavior keeps
    # LinkAddress.source/target aligned with Link.source/target, so the lower
    # address appears in a different field when the link is reversed.
    assert {str(forward_address.source.ip), str(forward_address.target.ip)} == {
        str(reverse_address.source.ip),
        str(reverse_address.target.ip),
    }
    assert (str(forward_address.source.ip), str(forward_address.target.ip)) == (
        "10.128.0.4",
        "10.128.0.5",
    )
    assert (str(reverse_address.source.ip), str(reverse_address.target.ip)) == (
        "10.128.0.5",
        "10.128.0.4",
    )
    assert forward_link.source is satellite_0
    assert reverse_link.source is reverse_satellite_1


def test_link_key_and_address_sort_key_are_orientation_independent(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    forward = manager.add_link(satellite_0, satellite_1)
    reverse = manager.add_link(satellite_1, satellite_0)

    assert (
        manager._link_key(forward)
        == manager._link_key(reverse)
        == frozenset(("Sat0", "Sat1"))
    )
    assert manager._address_link_sort_key(forward) == (0, 1)
    assert manager._address_link_sort_key(reverse) == (0, 1)


# ----------------------------------------------------------------------
# Loopback registration
# ----------------------------------------------------------------------


def test_add_loopback_interfaces_to_list_uses_plan_addresses(
    manager: _InterfaceMgrHost,
) -> None:
    satellite = manager.add_satellite("Sat0")
    ground = manager.add_ground_station("Gnd2")
    manager._initialize_address_plan()

    manager._add_loopback_interfaces_to_list()

    assert manager.interfaces == [satellite.loopback, ground.loopback]
    assert satellite.loopback.name == "lo_Sat0"
    assert ground.loopback.name == "lo_Gnd2"
    assert satellite.loopback.ipv4 == "10.128.0.1"
    assert ground.loopback.ipv4 == "10.0.0.1"


def test_add_loopback_interfaces_to_list_raises_without_plan_address(
    manager: _InterfaceMgrHost,
) -> None:
    manager.add_satellite("Sat0")

    with pytest.raises(RuntimeError, match="Loopback address for Sat0"):
        manager._add_loopback_interfaces_to_list()

    assert manager.interfaces == []


# ----------------------------------------------------------------------
# Link interface creation
# ----------------------------------------------------------------------


def test_build_interfaces_from_link_creates_peers_and_applies_plan_addresses(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    manager._initialize_address_plan()
    link = manager.add_link(satellite_0, satellite_1, distance_km=1000.0)

    manager._build_interfaces_from_link(link)

    assert len(link.peer_interfaces) == 2
    interface_1, interface_2 = link.peer_interfaces
    assert (interface_1.name, interface_2.name) == ("Sat0.1", "Sat1.0")
    assert (interface_1.get_iname(), interface_2.get_iname()) == ("eth1", "eth0")
    assert interface_1.peer is interface_2
    assert interface_2.peer is interface_1
    assert interface_1.type == interface_2.type == "InterSatelliteLink"
    assert interface_1.delay == interface_2.delay == link.delay
    assert interface_1.is_active is True
    assert interface_2.is_active is True
    assert manager.interfaces == [interface_1, interface_2]
    assert manager.interface_version == 1

    assert (interface_1.ipv4, interface_1.prefix_len) == ("10.128.0.4", 31)
    assert (interface_2.ipv4, interface_2.prefix_len) == ("10.128.0.5", 31)
    network_1 = ipaddress.IPv4Interface(
        f"{interface_1.ipv4}/{interface_1.prefix_len}"
    ).network
    network_2 = ipaddress.IPv4Interface(
        f"{interface_2.ipv4}/{interface_2.prefix_len}"
    ).network
    assert network_1 == network_2


def test_build_interfaces_from_link_marks_ground_station_links(
    manager: _InterfaceMgrHost,
) -> None:
    satellite = manager.add_satellite("Sat0")
    ground = manager.add_ground_station("Gnd2")
    manager._initialize_address_plan()
    link = manager.add_link(satellite, ground)

    manager._build_interfaces_from_link(link)

    assert [interface.type for interface in link.peer_interfaces] == [
        "GroundStationLink",
        "GroundStationLink",
    ]


def test_build_interfaces_from_link_is_idempotent(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    manager._initialize_address_plan()
    link = manager.add_link(satellite_0, satellite_1)

    manager._build_interfaces_from_link(link)
    interfaces = list(link.peer_interfaces)
    version = manager.interface_version

    manager._build_interfaces_from_link(link)

    assert link.peer_interfaces == interfaces
    assert link.peer_interfaces[0] is interfaces[0]
    assert link.peer_interfaces[1] is interfaces[1]
    assert manager.interfaces == interfaces
    assert manager.interface_version == version


def test_build_interfaces_without_set_ip_defers_address_allocation(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    manager._initialize_address_plan()
    link = manager.add_link(satellite_0, satellite_1)

    manager._build_interfaces_from_link(link, set_ip=False)

    assert [interface.ipv4 for interface in link.peer_interfaces] == ["", ""]
    assert _plan(manager).link_address(frozenset(("Sat0", "Sat1"))) is None


def test_build_interfaces_from_link_requires_an_address_plan(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    link = manager.add_link(satellite_0, satellite_1)

    with pytest.raises(RuntimeError, match="IPv4 address plan is not initialized"):
        manager._build_interfaces_from_link(link)

    # Interfaces are created before the missing plan is detected.
    assert len(link.peer_interfaces) == 2
    assert [interface.ipv4 for interface in link.peer_interfaces] == ["", ""]


def test_build_interfaces_syncs_batched_address_commands_when_running(
    manager: _InterfaceMgrHost,
) -> None:
    satellite_0 = manager.add_satellite("Sat0")
    satellite_1 = manager.add_satellite("Sat1")
    satellite_0_executor = FakeCommandExecutor()
    satellite_1_executor = FakeCommandExecutor()
    satellite_0.execute_command = satellite_0_executor
    satellite_1.execute_command = satellite_1_executor
    manager.status = True
    manager._initialize_address_plan()
    link = manager.add_link(satellite_0, satellite_1)

    manager._build_interfaces_from_link(link)

    interface_1, interface_2 = link.peer_interfaces
    assert _pushed_payload(satellite_0_executor) == (
        f"addr replace {interface_1.ipv4}/{interface_1.prefix_len}"
        f" dev {interface_1.get_iname()}"
    )
    assert _pushed_payload(satellite_1_executor) == (
        f"addr replace {interface_2.ipv4}/{interface_2.prefix_len}"
        f" dev {interface_2.get_iname()}"
    )


# ----------------------------------------------------------------------
# Bulk address push
# ----------------------------------------------------------------------


def test_set_ipv4s_for_all_nodes_dispatches_one_batch_per_node(
    manager: _InterfaceMgrHost,
) -> None:
    nodes = [
        manager.add_satellite("Sat0"),
        manager.add_satellite("Sat1"),
        manager.add_ground_station("Gnd2"),
    ]
    for node in nodes:
        node.set_ipv4_to_containers = MagicMock()

    manager.set_ipv4s_for_all_nodes(set_lo=False, max_workers=2)

    for node in nodes:
        node.set_ipv4_to_containers.assert_called_once_with(
            interface=None, set_lo=False
        )


def test_set_ipv4s_for_all_nodes_can_filter_node_kinds(
    manager: _InterfaceMgrHost,
) -> None:
    satellite = manager.add_satellite("Sat0")
    ground = manager.add_ground_station("Gnd2")
    satellite.set_ipv4_to_containers = MagicMock()
    ground.set_ipv4_to_containers = MagicMock()

    manager.set_ipv4s_for_all_nodes(
        satellites=False, ground_stations=True, max_workers=1
    )
    satellite.set_ipv4_to_containers.assert_not_called()
    ground.set_ipv4_to_containers.assert_called_once_with(interface=None, set_lo=True)

    satellite.set_ipv4_to_containers.reset_mock()
    ground.set_ipv4_to_containers.reset_mock()

    manager.set_ipv4s_for_all_nodes(
        satellites=True, ground_stations=False, max_workers=1
    )
    satellite.set_ipv4_to_containers.assert_called_once_with(
        interface=None, set_lo=True
    )
    ground.set_ipv4_to_containers.assert_not_called()


def test_set_ipv4s_for_all_nodes_attempts_every_node_before_raising(
    manager: _InterfaceMgrHost,
) -> None:
    failing = manager.add_satellite("Sat0")
    healthy = manager.add_satellite("Sat1")
    failing.set_ipv4_to_containers = MagicMock(
        side_effect=RuntimeError("transport down")
    )
    healthy.set_ipv4_to_containers = MagicMock()

    with pytest.raises(
        RuntimeError, match=r"IPv4 assignment failed on 1 node\(s\): Sat0"
    ):
        manager.set_ipv4s_for_all_nodes(ground_stations=False, max_workers=2)

    failing.set_ipv4_to_containers.assert_called_once_with(interface=None, set_lo=True)
    healthy.set_ipv4_to_containers.assert_called_once_with(interface=None, set_lo=True)


# ----------------------------------------------------------------------
# Peer-interface lookup
# ----------------------------------------------------------------------


def test_get_interface_to_peer_matches_numeric_name_suffix(
    manager: _InterfaceMgrHost,
) -> None:
    node = manager.add_satellite("Sat0")
    peer_7 = add_interface(node, "Sat0.7", ipv4="10.128.0.4")
    peer_3 = add_interface(node, "Sat0.3")
    add_interface(node, "Sat0.not-a-number")
    add_interface(node, "lo_Sat0")

    assert manager._get_interface_to_peer(node, 7) is peer_7
    assert manager._get_interface_to_peer(node, 3) is peer_3
    assert manager._get_interface_to_peer(node, 9) is None


def test_get_interface_to_peer_returns_none_for_node_without_interfaces(
    manager: _InterfaceMgrHost,
) -> None:
    assert manager._get_interface_to_peer(manager.add_satellite("Sat0"), 1) is None
