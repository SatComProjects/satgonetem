"""Tests for shared routing command builders."""

from __future__ import annotations

import base64

import pytest

from satgonetem.models.interface import Interface
from satgonetem.routing.commands import (
    assign_ips,
    build_ip_batch_command,
    enable_mpls,
    enable_mpls_commands,
    encode_ip_batch,
    format_net_address,
)
from tests.support import make_satellite

pytestmark = pytest.mark.unit


def _node_with_ips():
    node = make_satellite("Sat0")
    first = Interface(name="Sat0.1")
    first.set_ipv4("10.0.0.4")
    second = Interface(name="Sat0.2")
    second.set_ipv4("10.0.0.5", prefix_len=32)
    node.interfaces = [first, second]
    node.loopback = Interface(name="lo", iface_type="lo")
    node.loopback.set_ipv4("10.0.0.1")
    node.commands = []
    node.execute_command = lambda command, detach=False: node.commands.append(command)
    return node


def test_build_ip_batch_command_contains_all_addresses():
    node = _node_with_ips()

    command = build_ip_batch_command(node)
    encoded = command.split()[1]
    decoded = base64.b64decode(encoded).decode()

    assert decoded.splitlines() == [
        "addr replace 10.0.0.4/31 dev eth1",
        "addr replace 10.0.0.5/32 dev eth2",
        "addr replace 10.0.0.1/32 dev lo",
    ]
    assert command.endswith("| base64 -d | ip -force -batch -")


def test_build_ip_batch_command_returns_none_without_addresses():
    node = make_satellite("Sat0")
    node.loopback = Interface(name="lo", iface_type="lo")

    assert build_ip_batch_command(node) is None
    assign_ips(node)


def test_assign_ips_executes_one_batch():
    node = _node_with_ips()

    assign_ips(node)

    assert len(node.commands) == 1
    assert "ip -force -batch" in node.commands[0]


def test_encode_ip_batch_round_trips():
    command = encode_ip_batch(["route replace 10.0.0.0/8 via 10.0.0.1"])
    decoded = base64.b64decode(command.split()[1]).decode()

    assert decoded == "route replace 10.0.0.0/8 via 10.0.0.1"


def test_enable_mpls_commands_include_modules_and_interfaces():
    node = _node_with_ips()

    with_modules = enable_mpls_commands(node, load_modules=True)
    without_modules = enable_mpls_commands(node, load_modules=False)

    assert with_modules[0].startswith("modprobe mpls_router")
    assert not without_modules[0].startswith("modprobe")
    assert "sysctl -w net.mpls.platform_labels=1048575" in with_modules
    assert "sysctl -w net.mpls.conf.eth1.input=1" in with_modules
    assert "sysctl -w net.mpls.conf.eth2.input=1" in with_modules


def test_enable_mpls_executes_joined_command():
    node = _node_with_ips()

    enable_mpls(node, load_modules=False)

    assert len(node.commands) == 1
    assert "net.mpls.platform_labels" in node.commands[0]
    assert "modprobe" not in node.commands[0]


@pytest.mark.parametrize(
    "loopback, expected",
    [
        ("128.33.0.133", "49.0001.1280.3300.0133.00"),
        ("10.0.0.1", "49.0001.0100.0000.0001.00"),
    ],
)
def test_format_net_address(loopback, expected):
    assert format_net_address(loopback) == expected
