"""Tests for GoNetEmLauncher project-scoped container cleanup."""

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


@pytest.fixture
def launcher():
    instance = GoNetEmLauncher(
        topology_manager=MagicMock(),
        server_address="localhost:10110",
        project_name="test-project",
    )
    instance._client = MagicMock()
    instance._request = SimpleNamespace(id="abc123")
    instance._channel = MagicMock()
    return instance


def test_parse_project_containers_matches_exact_prefix():
    stdout = (
        "id1\tntmabc123.Sat0\n"
        "id2\tntmabc123.Controller\n"
        "id3\tntmabcdef.Sat0\n"
        "id4\tother-ntmabc123\n"
    )

    assert GoNetEmLauncher._parse_project_containers(stdout, "abc123") == ["id1", "id2"]


def test_force_close_only_stops_and_removes_scoped_containers(launcher):
    ps_result = _completed(
        ["docker", "ps"],
        stdout=(
            "id1\tntmabc123.Sat0\n"
            "id2\tntmabc123.Gnd0\n"
            "id3\tntmabcdef.Sat0\n"
            "id4\tunrelated-ntm-container\n"
        ),
    )
    docker_calls = []

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "ps", "-a"]:
            return ps_result
        docker_calls.append(command)
        return _completed(command)

    client = launcher._client
    channel = launcher._channel

    with patch(
        "satgonetem.launchers.gonetem_launcher.subprocess.run",
        side_effect=fake_run,
    ) as mock_run:
        launcher.force_close_project()

    ps_command = mock_run.call_args_list[0].args[0]
    assert ps_command == ["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}"]
    assert "name=ntm" not in ps_command

    assert ["docker", "stop", "-t", "0", "id1"] in docker_calls
    assert ["docker", "stop", "-t", "0", "id2"] in docker_calls
    assert ["docker", "rm", "id1"] in docker_calls
    assert ["docker", "rm", "id2"] in docker_calls
    assert all("id3" not in command and "id4" not in command for command in docker_calls)

    client.ProjectClose.assert_called_once_with(launcher._request)
    channel.close.assert_called_once()
    assert launcher._client is None
    assert launcher._channel is None


def test_force_close_skips_docker_when_project_id_is_empty(launcher):
    launcher._request.id = ""
    client = launcher._client
    channel = launcher._channel

    with patch(
        "satgonetem.launchers.gonetem_launcher.subprocess.run"
    ) as mock_run:
        launcher.force_close_project()

    mock_run.assert_not_called()
    client.ProjectClose.assert_not_called()
    channel.close.assert_called_once()


def test_force_close_still_closes_project_when_docker_ps_fails(launcher):
    ps_result = _completed(["docker", "ps"], returncode=1, stderr="docker unavailable")
    client = launcher._client
    channel = launcher._channel

    with patch(
        "satgonetem.launchers.gonetem_launcher.subprocess.run",
        return_value=ps_result,
    ) as mock_run:
        launcher.force_close_project()

    assert mock_run.call_count == 1
    client.ProjectClose.assert_called_once_with(launcher._request)
    channel.close.assert_called_once()


def test_force_close_attempts_remove_even_when_stop_fails(launcher):
    ps_result = _completed(["docker", "ps"], stdout="id1\tntmabc123.Sat0\n")
    docker_calls = []

    def fake_run(command, **kwargs):
        if command[:3] == ["docker", "ps", "-a"]:
            return ps_result
        docker_calls.append(command)
        if command[1] == "stop":
            return _completed(command, returncode=1, stderr="stop failed")
        return _completed(command)

    with patch(
        "satgonetem.launchers.gonetem_launcher.subprocess.run",
        side_effect=fake_run,
    ):
        launcher.force_close_project()

    assert ["docker", "stop", "-t", "0", "id1"] in docker_calls
    assert ["docker", "rm", "id1"] in docker_calls
