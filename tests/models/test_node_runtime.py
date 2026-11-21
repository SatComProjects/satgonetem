"""Tests for Node container/namespace port injection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from satgonetem.ports import DockerContainerRuntime
from tests.support import make_satellite

pytestmark = pytest.mark.unit


def test_node_execute_command_docker_uses_container_runtime():
    node = make_satellite("Sat0")
    node.container = object()
    runtime = MagicMock()
    runtime.exec.return_value = (0, b"ok\n")
    node.container_runtime = runtime

    assert node.execute_command_docker("uptime") == "ok"
    runtime.exec.assert_called_once()
    assert runtime.exec.call_args.args[1] == ["sh", "-c", "uptime"]


def test_node_execute_command_docker_propagates_exit_code():
    node = make_satellite("Sat0")
    node.container = object()
    runtime = MagicMock()
    runtime.exec.return_value = (2, b"bad")
    node.container_runtime = runtime

    with pytest.raises(RuntimeError, match="exit 2"):
        node.execute_command_docker("false")


def test_create_dummy_interface_delegates_to_namespace():
    node = make_satellite("Sat0")
    node.container = object()
    node.container_runtime = MagicMock()
    node.container_runtime.pid.return_value = 123
    node.network_namespace = MagicMock()

    node.create_dummy_interface("dummy0")

    assert node.container_pid == 123
    node.network_namespace.create_dummy_interface.assert_called_once_with(123, "dummy0")


def test_connect_to_delegates_to_namespace():
    source = make_satellite("Sat0")
    target = make_satellite("Sat1")
    source.container_pid = 10
    target.container_pid = 11
    source.network_namespace = MagicMock()

    source.connect_to(target, "eth0", "eth1")

    source.network_namespace.create_veth.assert_called_once_with(10, 11, "eth0", "eth1")


def test_docker_container_runtime_uses_container_attrs_and_exec():
    container = MagicMock()
    container.attrs = {"State": {"Pid": "42"}}
    container.exec_run.return_value = (0, b"out")
    runtime = DockerContainerRuntime()

    assert runtime.pid(container) == 42
    assert runtime.exec(container, ["true"]) == (0, b"out")
    container.exec_run.assert_called_once_with(cmd=["true"], detach=False)
