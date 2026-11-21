"""Characterization tests for ControllerMixin helpers and command construction."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from satgonetem.services.mixins.controller import ControllerMixin
from tests.support import DummyTopology

pytestmark = pytest.mark.unit


class _ControllerHost(ControllerMixin, DummyTopology):
    """Lightweight topology host exposing ControllerMixin."""


@pytest.fixture
def controller_host() -> _ControllerHost:
    host = _ControllerHost(
        controller_veth_host="veth-host",
        controller_veth_peer="veth-controller",
        controller_host_ip="248.0.0.2",
        controller_subnet_prefix=16,
        controller_bridge_name="Controller",
    )
    host.command_runner = MagicMock()
    return host


def _completed(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    """Minimal stand-in for a subprocess.CompletedProcess result."""
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ----------------------------------------------------------------------
# Pure address/interface helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        (0, "248.0.0.1"),
        (1, "248.0.0.3"),
        (87, "248.0.0.175"),
        (127, "248.0.0.255"),
        (128, "248.0.1.1"),
        (8191, "248.0.63.255"),
    ],
)
def test_build_controller_ip_encodes_node_id_in_low_bits(
    controller_host: _ControllerHost, node_id: int, expected: str
) -> None:
    assert controller_host.build_controller_ip(node_id) == expected


@pytest.mark.parametrize("node_id", [-1, 8192, 1.5, "1", None])
def test_build_controller_ip_rejects_ids_outside_13_bits(
    controller_host: _ControllerHost, node_id: object
) -> None:
    with pytest.raises(ValueError, match=r"\[0, 8191\]"):
        controller_host.build_controller_ip(node_id)  # type: ignore[arg-type]


def test_build_satellite_ip_is_an_alias_for_build_controller_ip(
    controller_host: _ControllerHost,
) -> None:
    assert controller_host.build_satellite_ip(87) == "248.0.0.175"


@pytest.mark.parametrize(
    ("node_id", "expected"), [(0, "248.0.0.1"), (87, "248.0.0.175")]
)
def test_get_management_ip_matches_controller_address(
    controller_host: _ControllerHost, node_id: int, expected: str
) -> None:
    assert controller_host.get_management_ip(node_id) == expected


@pytest.mark.parametrize(
    ("node_id", "expected"), [(0, "eth50000"), (87, "eth50087")]
)
def test_get_management_interface_uses_50000_offset(
    controller_host: _ControllerHost, node_id: int, expected: str
) -> None:
    assert controller_host.get_management_interface(node_id) == expected


def test_get_management_interface_formats_controller_node_iface_pattern() -> None:
    host = _ControllerHost(controller_node_iface_pattern="mgmt{node_id}")

    assert host.get_management_interface(7) == "mgmt50007"


# ----------------------------------------------------------------------
# Controller veth command construction
# ----------------------------------------------------------------------


def test_remove_controller_veth_removes_ovs_port_then_host_link(
    controller_host: _ControllerHost,
) -> None:
    run = controller_host.command_runner.run
    controller_host._remove_controller_veth(
        "veth-host", "veth-controller", "Controller", ovs_cid="ovs-123"
    )

    assert run.call_args_list == [
        call(
            [
                "docker",
                "exec",
                "ovs-123",
                "ovs-vsctl",
                "--if-exists",
                "del-port",
                "Controller",
                "veth-controller",
            ],
            capture_output=True,
            text=True,
        ),
        call(
            ["sudo", "ip", "link", "delete", "veth-host"],
            capture_output=True,
            text=True,
        ),
    ]


def test_remove_controller_veth_without_container_only_deletes_host_link(
    controller_host: _ControllerHost,
) -> None:
    run = controller_host.command_runner.run
    controller_host._remove_controller_veth(
        "veth-host", "veth-controller", "Controller", ovs_cid=None
    )

    run.assert_called_once_with(
        ["sudo", "ip", "link", "delete", "veth-host"],
        capture_output=True,
        text=True,
    )


def test_setup_controller_connection_builds_full_veth_command_sequence(
    controller_host: _ControllerHost,
) -> None:
    results = [
        _completed("ovs-abc\n"),
        _completed(),
        _completed(),
        _completed("4242\n"),
        _completed(),
        _completed(),
        _completed(),
        _completed(),
    ]

    run = controller_host.command_runner.run
    run.side_effect = results
    controller_host.setup_controller_connection()

    commands = [invocation.args[0] for invocation in run.call_args_list]
    assert commands == [
        ["docker", "ps", "--filter", "name=.ovs", "--format", "{{.ID}}"],
        [
            "sudo",
            "ip",
            "link",
            "add",
            "veth-host",
            "type",
            "veth",
            "peer",
            "name",
            "veth-controller",
        ],
        ["sudo", "ip", "link", "set", "veth-host", "up"],
        ["docker", "inspect", "-f", "{{.State.Pid}}", "ovs-abc"],
        ["sudo", "ip", "link", "set", "veth-controller", "netns", "4242"],
        ["docker", "exec", "ovs-abc", "ip", "link", "set", "veth-controller", "up"],
        [
            "docker",
            "exec",
            "ovs-abc",
            "ovs-vsctl",
            "--may-exist",
            "add-port",
            "Controller",
            "veth-controller",
        ],
        [
            "sudo",
            "ip",
            "addr",
            "add",
            "248.0.0.2/16",
            "dev",
            "veth-host",
        ],
    ]
    check_flags = [
        invocation.kwargs.get("check", False) for invocation in run.call_args_list
    ]
    assert check_flags == [False, True, True, True, True, True, True, True]
    assert all(
        invocation.kwargs["capture_output"] is True
        and invocation.kwargs["text"] is True
        for invocation in run.call_args_list
    )


@pytest.mark.parametrize(
    "result",
    [
        _completed("", returncode=1),
        _completed(""),
        _completed("  \n"),
    ],
)
def test_setup_controller_connection_requires_an_ovs_container(
    controller_host: _ControllerHost, result: SimpleNamespace
) -> None:
    run = controller_host.command_runner.run
    run.return_value = result
    with pytest.raises(RuntimeError, match="Could not find an OVS container"):
        controller_host.setup_controller_connection()

    run.assert_called_once()
    assert run.call_args.args[0] == [
        "docker",
        "ps",
        "--filter",
        "name=.ovs",
        "--format",
        "{{.ID}}",
    ]


def test_setup_controller_connection_rolls_back_when_a_command_fails(
    controller_host: _ControllerHost,
) -> None:
    failure = subprocess.CalledProcessError(
        returncode=1,
        cmd=["sudo", "ip", "link", "add"],
        stderr="boom\n",
    )

    run = controller_host.command_runner.run
    run.side_effect = [_completed("ovs-abc\n"), failure]
    with patch.object(controller_host, "_remove_controller_veth") as rollback:
        with pytest.raises(
            RuntimeError, match="Failed to set up controller connection: boom"
        ):
            controller_host.setup_controller_connection()

    assert run.call_count == 2
    rollback.assert_called_once_with(
        "veth-host", "veth-controller", "Controller", "ovs-abc"
    )


# ----------------------------------------------------------------------
# Controller cleanup
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected_cid"),
    [
        (0, "ovs-abc\n", "ovs-abc"),
        (0, "ovs-abc\novs-def\n", "ovs-abc"),
        (0, "", None),
        (1, "ovs-abc\n", None),
    ],
)
def test_cleanup_controller_connection_passes_discovered_container_id(
    controller_host: _ControllerHost,
    returncode: int,
    stdout: str,
    expected_cid: str | None,
) -> None:
    result = _completed(stdout, returncode=returncode)

    controller_host.command_runner.run.return_value = result
    with patch.object(controller_host, "_remove_controller_veth") as remove:
        controller_host.cleanup_controller_connection()

    remove.assert_called_once_with(
        "veth-host", "veth-controller", "Controller", expected_cid
    )
