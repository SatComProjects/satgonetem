"""Regression tests for LAUNCH-02: honor ProjectOpen/version response status."""

from unittest.mock import MagicMock, patch

import pytest

from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
from satgonetem.proto import netem_pb2


@pytest.fixture
def launcher():
    instance = GoNetEmLauncher(
        topology_manager=MagicMock(),
        server_address="localhost:10110",
        project_name="test-project",
    )
    instance._client = MagicMock()
    instance._request = None
    instance._channel = MagicMock()
    return instance


def _status(code, error=""):
    return netem_pb2.Status(code=code, error=error)


def _version_response(code=netem_pb2.StatusCode.OK, error=""):
    return netem_pb2.VersionResponse(
        status=_status(code, error), version="test-version"
    )


def _open_response(code=netem_pb2.StatusCode.OK, error="", project_id="abc123"):
    return netem_pb2.PrjOpenResponse(
        status=_status(code, error), id=project_id
    )


def _start(launcher, project_file):
    with patch.object(launcher, "_get_client", return_value=launcher._client), patch.object(
        launcher, "generate_project", return_value=str(project_file)
    ), patch.object(launcher, "_attach_containers"):
        return launcher.start_containers([])


def test_project_open_error_status_with_non_empty_id_raises(launcher, tmp_path):
    project_file = tmp_path / "project.gnet"
    project_file.write_bytes(b"fake-project")
    client = launcher._client
    client.ServerGetVersion.return_value = _version_response()
    client.ProjectOpen.return_value = _open_response(
        netem_pb2.StatusCode.ERROR,
        "project open exploded",
        project_id="abc123",
    )

    with pytest.raises(RuntimeError) as excinfo:
        _start(launcher, project_file)

    message = str(excinfo.value)
    assert "ProjectOpen failed" in message
    assert "project open exploded" in message
    # The failed open must not be treated as a running project.
    client.TopologyRun.assert_not_called()
    client.ProjectClose.assert_not_called()
    assert launcher._request is None
    assert launcher._client is None

    assert not project_file.exists()


def test_project_open_ok_status_still_starts_topology(launcher, tmp_path):
    project_file = tmp_path / "project.gnet"
    project_file.write_bytes(b"fake-project")
    client = launcher._client
    client.ServerGetVersion.return_value = _version_response()
    client.ProjectOpen.return_value = _open_response(
        netem_pb2.StatusCode.OK, project_id="abc123"
    )
    client.TopologyRun.return_value = []

    container_time, link_time = _start(launcher, project_file)

    assert (container_time, link_time) == (0.0, 0.0)
    client.TopologyRun.assert_called_once()
    assert launcher._request.id == "abc123"
    assert not project_file.exists()


def test_server_get_version_error_status_raises(launcher, tmp_path):
    project_file = tmp_path / "project.gnet"
    client = launcher._client
    client.ServerGetVersion.return_value = _version_response(
        netem_pb2.StatusCode.ERROR, "version unavailable"
    )

    with pytest.raises(RuntimeError) as excinfo:
        _start(launcher, project_file)

    message = str(excinfo.value)
    assert "ServerGetVersion failed" in message
    assert "version unavailable" in message
    client.ProjectOpen.assert_not_called()
    client.TopologyRun.assert_not_called()
    assert launcher._client is None
