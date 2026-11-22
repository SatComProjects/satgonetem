"""Behavioral tests for GoNetEm client, cleanup, and registry edge paths."""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from satgonetem.launchers.gonetem_cleanup import FORCE_CLOSE_TIMEOUT_SECONDS
from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
from satgonetem.launchers.registry import (
    available_launchers,
    create_launcher,
    register_launcher,
)
from satgonetem.proto import netem_pb2


def _launcher(**kwargs) -> GoNetEmLauncher:
    return GoNetEmLauncher(
        topology_manager=MagicMock(),
        server_address="localhost:10110",
        project_name="test-project",
        **kwargs,
    )


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


def _link(**overrides):
    link = SimpleNamespace(
        source=SimpleNamespace(id=0, name="Sat0"),
        target=SimpleNamespace(id=1, name="Sat1"),
        delay=10,
        loss=0,
        jitter=0,
        type="InterSatelliteLink",
        capacity=0,
        peer1_capacity=0,
        peer2_capacity=0,
    )
    for key, value in overrides.items():
        setattr(link, key, value)
    return link


# Link request capacity selection


@pytest.mark.parametrize(
    ("link_type", "expected"),
    [
        ("InterSatelliteLink", 101),
        ("GroundStationLink", 202),
        ("GroundObjectLink", 303),
        ("UnknownLink", 101),
    ],
)
def test_link_capacities_fall_back_to_type_specific_defaults(link_type, expected):
    launcher = _launcher(
        isl_capacity_kbps=101,
        gnd_capacity_kbps=202,
        ground_object_capacity_kbps=303,
    )
    link = _link(type=link_type)

    assert launcher._link_capacities_for(link) == (expected, expected)


def test_link_capacities_prefer_peer_then_link_capacity():
    launcher = _launcher(isl_capacity_kbps=101)

    assert launcher._link_capacities_for(
        _link(peer1_capacity=0, peer2_capacity=222, capacity=333)
    ) == (333, 222)


# Transport close


def test_close_transport_logs_connection_close_failure(caplog):
    launcher = _launcher()
    connection = MagicMock()
    connection.close.side_effect = RuntimeError("connection close failed")
    launcher._connection = connection
    channel = launcher._channel = MagicMock()
    launcher._client = MagicMock()

    with caplog.at_level(logging.WARNING):
        launcher._close_transport()

    assert "failed to close gRPC connection: connection close failed" in caplog.text
    channel.close.assert_not_called()
    assert launcher._connection is None
    assert launcher._channel is None
    assert launcher._client is None


def test_close_transport_logs_channel_close_failure(caplog):
    launcher = _launcher()
    launcher._connection = None
    channel = launcher._channel = MagicMock()
    channel.close.side_effect = RuntimeError("channel close failed")
    launcher._client = MagicMock()

    with caplog.at_level(logging.WARNING):
        launcher._close_transport()

    assert "failed to close gRPC channel: channel close failed" in caplog.text
    assert launcher._connection is None
    assert launcher._channel is None
    assert launcher._client is None


# Docker cleanup


def test_parse_project_containers_skips_malformed_lines():
    stdout = (
        "id1\tntmprj.Sat0\nmalformed-without-tab\nid2\tntmprj\nid3\tother-container\n\n"
    )

    assert GoNetEmLauncher._parse_project_containers(stdout, "prj") == ["id1", "id2"]


def test_run_docker_command_reports_oserror(caplog):
    launcher = _launcher()
    launcher._command_runner = MagicMock()
    launcher._command_runner.run.side_effect = OSError("docker missing")

    with caplog.at_level(logging.ERROR):
        succeeded = launcher._run_docker_command(["docker", "rm", "id1"])

    assert succeeded is False
    assert "failed to run docker rm id1: docker missing" in caplog.text


def test_list_project_containers_returns_empty_on_oserror(caplog):
    launcher = _launcher()
    launcher._command_runner = MagicMock()
    launcher._command_runner.run.side_effect = OSError("cannot execute docker")

    with caplog.at_level(logging.ERROR):
        containers = launcher._list_project_containers("prj")

    assert containers == []
    assert "could not list Docker containers: cannot execute docker" in caplog.text


def test_force_close_requires_client_and_request():
    launcher = _launcher()
    launcher._command_runner = MagicMock()
    launcher._request = None
    launcher._client = MagicMock()

    launcher.force_close_project()

    launcher._command_runner.run.assert_not_called()
    launcher._client.ProjectClose.assert_not_called()

    launcher._client = None
    launcher._request = netem_pb2.ProjectRequest(id="prj")
    launcher.force_close_project()

    launcher._command_runner.run.assert_not_called()


def test_force_close_logs_project_close_failure_and_closes_transport(caplog):
    launcher = _launcher()
    launcher._command_runner = MagicMock()
    launcher._command_runner.run.return_value = _completed()
    launcher._client = MagicMock()
    launcher._client.ProjectClose.side_effect = RuntimeError("close exploded")
    launcher._request = netem_pb2.ProjectRequest(id="prj")
    connection = launcher._connection = MagicMock()

    with caplog.at_level(logging.ERROR):
        launcher.force_close_project()

    assert "force_close_project failed: close exploded" in caplog.text
    connection.close.assert_called_once()
    assert launcher._connection is None
    assert launcher._channel is None
    assert launcher._client is None


def test_force_close_drains_stream_before_closing_transport():
    launcher = _launcher()
    launcher._client = MagicMock()
    launcher._command_runner = MagicMock()
    launcher._command_runner.run.return_value = _completed()
    launcher._request = netem_pb2.ProjectRequest(id="prj")
    channel = launcher._channel = MagicMock()
    events = []

    def stream():
        events.append("first")
        yield netem_pb2.ProjectCloseMsg(
            code=netem_pb2.ProjectCloseMsg.NODE_COUNT, total=1
        )
        events.append("second")
        yield netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_CLOSE)
        events.append("exhausted")

    launcher._client.ProjectClose.return_value = stream()
    channel.close.side_effect = lambda: events.append("transport-closed")
    client = launcher._client

    launcher.force_close_project()

    assert events == ["first", "second", "exhausted", "transport-closed"]
    client.ProjectClose.assert_called_once_with(
        launcher._request, timeout=FORCE_CLOSE_TIMEOUT_SECONDS
    )
    assert launcher._client is None
    assert launcher._channel is None


def test_force_close_still_closes_transport_when_stream_raises(caplog):
    launcher = _launcher()
    launcher._client = MagicMock()
    launcher._command_runner = MagicMock()
    launcher._command_runner.run.return_value = _completed()
    launcher._request = netem_pb2.ProjectRequest(id="prj")
    channel = launcher._channel = MagicMock()

    def stream():
        yield None
        raise RuntimeError("stream exploded")

    launcher._client.ProjectClose.return_value = stream()

    with caplog.at_level(logging.ERROR):
        launcher.force_close_project()

    assert "force_close_project failed: stream exploded" in caplog.text
    channel.close.assert_called_once()
    assert launcher._client is None
    assert launcher._channel is None


# Registry


def test_available_launchers_lists_builtin_first_and_deduplicates_names():
    register_launcher("OPS_LANE_BACKEND", object)
    register_launcher("ops_lane_backend", object)

    names = available_launchers()

    assert names[0] == "GONETEM"
    assert names.count("GONETEM") == 1
    assert names.count("OPS_LANE_BACKEND") == 1


def test_create_launcher_forwards_kwargs_to_registered_factory():
    sentinel = object()
    received = {}

    def factory(**kwargs):
        received.update(kwargs)
        return sentinel

    register_launcher("OPS_LANE_FACTORY", factory)

    result = create_launcher("ops_lane_factory", project_name="p", workers=3)

    assert result is sentinel
    assert received == {"project_name": "p", "workers": 3}


def test_create_launcher_defaults_empty_name_to_builtin():
    launcher = create_launcher(
        "",
        topology_manager=MagicMock(),
        server_address="localhost:10110",
        project_name="test-project",
    )

    assert isinstance(launcher, GoNetEmLauncher)
