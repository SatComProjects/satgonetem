"""Regression tests for M08: controller setup robustness.

Controller addressing must be retry-safe, setup must roll back on any command
failure (not only CalledProcessError), and veth removal must report failures
instead of silently claiming success.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _setup_results() -> list[SimpleNamespace]:
    return [
        _completed("ovs-abc\n"),
        _completed(),
        _completed(),
        _completed("4242\n"),
        _completed(),
        _completed(),
        _completed(),
        _completed(),
    ]


def test_setup_uses_addr_replace_so_a_retry_is_idempotent(controller_host):
    controller_host.command_runner.run.side_effect = _setup_results()

    controller_host.setup_controller_connection()

    commands = [
        invocation.args[0]
        for invocation in controller_host.command_runner.run.call_args_list
    ]
    assert commands[-1] == [
        "sudo",
        "ip",
        "addr",
        "replace",
        "248.0.0.2/16",
        "dev",
        "veth-host",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("permission denied"),
        PermissionError("sudo is not permitted"),
        subprocess.TimeoutExpired(cmd=["sudo", "ip", "link", "add"], timeout=5),
    ],
)
def test_setup_rolls_back_on_non_called_process_failures(controller_host, failure):
    controller_host.command_runner.run.side_effect = [
        _completed("ovs-abc\n"),
        failure,
    ]

    with patch.object(controller_host, "_remove_controller_veth") as rollback:
        with pytest.raises(
            RuntimeError, match="Failed to set up controller connection"
        ):
            controller_host.setup_controller_connection()

    rollback.assert_called_once_with(
        "veth-host", "veth-controller", "Controller", "ovs-abc"
    )


def test_setup_rollback_failure_does_not_mask_the_setup_error(controller_host):
    controller_host.command_runner.run.side_effect = [
        _completed("ovs-abc\n"),
        OSError("permission denied"),
    ]

    with patch.object(
        controller_host,
        "_remove_controller_veth",
        side_effect=RuntimeError("rollback boom"),
    ):
        with pytest.raises(RuntimeError, match="permission denied"):
            controller_host.setup_controller_connection()


def test_remove_controller_veth_reports_a_failed_delete(controller_host, caplog):
    controller_host.command_runner.run.side_effect = [
        _completed(),
        _completed(stderr="RTNETLINK answers: Operation not permitted", returncode=1),
    ]

    removed = controller_host._remove_controller_veth(
        "veth-host", "veth-controller", "Controller", ovs_cid="ovs-abc"
    )

    assert removed is False
    assert "Operation not permitted" in caplog.text


def test_remove_controller_veth_treats_a_missing_link_as_already_removed(
    controller_host, caplog
):
    controller_host.command_runner.run.side_effect = [
        _completed(),
        _completed(stderr='Cannot find device "veth-host"', returncode=1),
    ]

    removed = controller_host._remove_controller_veth(
        "veth-host", "veth-controller", "Controller", ovs_cid="ovs-abc"
    )

    assert removed is True
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_remove_controller_veth_logs_ovs_port_failure_but_deletes_host_link(
    controller_host, caplog
):
    controller_host.command_runner.run.side_effect = [
        _completed(stderr="Error: no such bridge", returncode=1),
        _completed(),
    ]

    removed = controller_host._remove_controller_veth(
        "veth-host", "veth-controller", "Controller", ovs_cid="ovs-abc"
    )

    assert removed is True
    assert "no such bridge" in caplog.text
    assert controller_host.command_runner.run.call_args.args[0] == [
        "sudo",
        "ip",
        "link",
        "delete",
        "veth-host",
    ]


def test_setup_controller_addressing_uses_replace(controller_host):
    satellite = controller_host.add_satellite("Sat0")
    satellite.execute_command = MagicMock()
    expected_ip = controller_host.build_controller_ip(satellite.id)
    expected_iface = controller_host.get_management_interface(satellite.id)

    assigned = controller_host.setup_controller_addressing()

    satellite.execute_command.assert_called_once_with(
        f"ip addr replace {expected_ip}/16 dev {expected_iface}"
    )
    assert assigned == {"Sat0": expected_ip}


def test_cleanup_does_not_claim_success_when_veth_deletion_fails(
    controller_host, caplog
):
    controller_host.command_runner.run.side_effect = [
        _completed("ovs-abc\n"),
        _completed(),
        _completed(stderr="RTNETLINK answers: Operation not permitted", returncode=1),
    ]

    with caplog.at_level("INFO"):
        controller_host.cleanup_controller_connection()

    assert "cleaned up" not in caplog.text
    assert "may be incomplete" in caplog.text
