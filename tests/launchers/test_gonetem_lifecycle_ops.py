"""Behavioral tests for GoNetEm lifecycle operations.

These cover the TopologyRun/ProjectClose streaming contracts, project-file
cleanup, container attachment, and the per-link mutation paths.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import grpc
import pytest

from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
from satgonetem.proto import netem_pb2


def _new_launcher(**kwargs) -> GoNetEmLauncher:
    """Build a launcher whose topology manager is an empty mapping set."""
    topology_manager = MagicMock()
    topology_manager.satellites = {}
    topology_manager.ground_stations = {}
    topology_manager.links = {}
    return GoNetEmLauncher(
        topology_manager=topology_manager,
        server_address="localhost:10110",
        project_name="test-project",
        **kwargs,
    )


def _start_launcher(tmp_path, client):
    """Build a launcher wired to *client* through the injected factory."""
    connection = SimpleNamespace(channel=MagicMock(), client=client, close=MagicMock())
    factory = SimpleNamespace(connect=MagicMock(return_value=connection))
    launcher = _new_launcher(
        temp_path=str(tmp_path),
        client_factory=factory,
    )
    return launcher, connection, factory


def _run_msg(code, total: int = 0) -> netem_pb2.TopologyRunMsg:
    return netem_pb2.TopologyRunMsg(code=code, total=total)


def _link():
    return SimpleNamespace(
        source=SimpleNamespace(id=0, name="Sat0"),
        target=SimpleNamespace(id=1, name="Sat1"),
        delay=10,
        loss=0,
        jitter=0,
        type="InterSatelliteLink",
        peer1_capacity=1000,
        peer2_capacity=1000,
    )


def _ack(code=netem_pb2.StatusCode.OK, error=""):
    return netem_pb2.AckResponse(status=netem_pb2.Status(code=code, error=error))


@pytest.fixture
def grpc_client():
    client = MagicMock()
    client.ServerGetVersion.return_value = netem_pb2.VersionResponse(
        status=netem_pb2.Status(code=netem_pb2.StatusCode.OK),
        version="1.0.0",
    )
    client.ProjectOpen.return_value = netem_pb2.PrjOpenResponse(
        status=netem_pb2.Status(code=netem_pb2.StatusCode.OK),
        id="prj-1",
    )
    client.TopologyRun.return_value = []
    return client


# start_containers


def test_start_containers_reports_progress_for_every_topology_event(
    grpc_client, tmp_path
):
    grpc_client.TopologyRun.return_value = [
        _run_msg(netem_pb2.TopologyRunMsg.NODE_COUNT, 2),
        _run_msg(netem_pb2.TopologyRunMsg.BRIDGE_COUNT, 1),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_COUNT, 2),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_START),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_START),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_SETUP),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_SETUP),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_LOADCONFIG),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_STOP),
    ]
    launcher, connection, factory = _start_launcher(tmp_path, grpc_client)
    events = []

    container_time, link_time = launcher.start_containers(
        [], progress_cb=lambda *event: events.append(event)
    )

    assert events == [
        ("NODE_COUNT", 0, 2),
        ("LINK_COUNT", 0, 2),
        ("NODE_START", 1, 2),
        ("NODE_START", 2, 2),
        ("LINK_SETUP", 1, 2),
        ("LINK_SETUP", 2, 2),
        ("NODE_LOADCONFIG", 1, 2),
        ("COMPLETED", 0, 0),
    ]
    assert container_time >= 0.0
    assert link_time >= 0.0

    factory.connect.assert_called_once_with("localhost:10110")
    grpc_client.TopologyRun.assert_called_once()
    assert launcher._request.id == "prj-1"
    assert launcher._topology_manager.project_id == "prj-1"
    assert launcher._topology_manager.client is grpc_client
    assert launcher._topology_manager.request is launcher._request
    # A running project keeps its transport open and removes the archive.
    connection.close.assert_not_called()
    assert not list(tmp_path.glob("*.gnet"))


def test_start_containers_counts_batched_progress_totals(grpc_client, tmp_path):
    grpc_client.TopologyRun.return_value = [
        _run_msg(netem_pb2.TopologyRunMsg.NODE_COUNT, 3),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_COUNT, 2),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_START, 2),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_START, 1),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_SETUP, 2),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_LOADCONFIG, 3),
    ]
    launcher, _connection, _factory = _start_launcher(tmp_path, grpc_client)
    events = []

    launcher.start_containers([], progress_cb=lambda *event: events.append(event))

    assert ("NODE_START", 3, 3) in events
    assert ("LINK_SETUP", 2, 2) in events


def test_start_containers_accepts_terminal_marker_with_short_progress(
    grpc_client, tmp_path
):
    grpc_client.TopologyRun.return_value = [
        _run_msg(netem_pb2.TopologyRunMsg.NODE_COUNT, 3),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_COUNT, 2),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_START, 1),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_MESSAGES),
    ]
    launcher, _connection, _factory = _start_launcher(tmp_path, grpc_client)

    launcher.start_containers([])


def test_start_containers_allows_omitting_progress_callback(grpc_client, tmp_path):
    grpc_client.TopologyRun.return_value = [
        _run_msg(netem_pb2.TopologyRunMsg.NODE_COUNT, 1),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_COUNT, 1),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_START),
        _run_msg(netem_pb2.TopologyRunMsg.LINK_SETUP),
        _run_msg(netem_pb2.TopologyRunMsg.NODE_LOADCONFIG),
    ]
    launcher, _connection, _factory = _start_launcher(tmp_path, grpc_client)

    container_time, link_time = launcher.start_containers([])

    assert container_time >= 0.0
    assert link_time >= 0.0


def test_start_containers_ignores_project_file_already_removed(
    grpc_client, tmp_path, monkeypatch
):
    project_file = tmp_path / "project.gnet"
    project_file.write_bytes(b"project")
    launcher, _connection, _factory = _start_launcher(tmp_path, grpc_client)
    monkeypatch.setattr(
        launcher, "generate_project", lambda *args, **kwargs: str(project_file)
    )
    monkeypatch.setattr(
        "satgonetem.launchers.gonetem_lifecycle.os.unlink",
        MagicMock(side_effect=FileNotFoundError),
    )

    assert launcher.start_containers([]) == (0.0, 0.0)


def test_start_containers_logs_unlink_oserror_and_keeps_running(
    grpc_client, tmp_path, monkeypatch, caplog
):
    project_file = tmp_path / "project.gnet"
    project_file.write_bytes(b"project")
    launcher, _connection, _factory = _start_launcher(tmp_path, grpc_client)
    monkeypatch.setattr(
        launcher, "generate_project", lambda *args, **kwargs: str(project_file)
    )
    monkeypatch.setattr(
        "satgonetem.launchers.gonetem_lifecycle.os.unlink",
        MagicMock(side_effect=OSError("device busy")),
    )

    with caplog.at_level(logging.WARNING):
        result = launcher.start_containers([])

    assert result == (0.0, 0.0)
    assert "could not remove project file" in caplog.text
    assert "device busy" in caplog.text
    assert project_file.exists()


def test_cleanup_failed_start_retains_state_when_force_close_fails(caplog):
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    connection = SimpleNamespace(close=MagicMock())
    launcher._connection = connection
    launcher._channel = MagicMock()
    launcher.force_close_project = MagicMock(
        side_effect=RuntimeError("forced close failed")
    )

    with caplog.at_level(logging.ERROR):
        assert launcher._cleanup_failed_start() is False

    assert "cleanup after failed start failed: forced close failed" in caplog.text
    connection.close.assert_not_called()
    assert launcher._request is not None
    assert launcher._client is not None


def test_cleanup_failed_start_clears_state_after_successful_retry():
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    connection = SimpleNamespace(close=MagicMock())
    launcher._connection = connection
    launcher._channel = MagicMock()
    launcher.force_close_project = MagicMock(return_value=False)

    assert launcher._cleanup_failed_start() is False
    assert launcher._request is not None
    connection.close.assert_not_called()

    launcher.force_close_project.return_value = True
    assert launcher._cleanup_failed_start() is True

    connection.close.assert_called_once()
    assert launcher._request is None
    assert launcher._client is None


# Container attachment


def test_attach_containers_logs_missing_containers_and_continues(caplog):
    runtime = MagicMock()
    runtime.get.side_effect = [
        RuntimeError("not found"),
        SimpleNamespace(attrs={"State": {}}),
        SimpleNamespace(attrs={"State": {"Pid": "4321"}}),
    ]
    launcher = _new_launcher(container_runtime=runtime)
    launcher._client = MagicMock()
    nodes = [
        SimpleNamespace(name="Sat0", container="stale", container_pid=99),
        SimpleNamespace(name="Sat1", container=None, container_pid=None),
        SimpleNamespace(name="Gnd0", container=None, container_pid=None),
    ]

    with caplog.at_level(logging.INFO):
        launcher._attach_containers(nodes, "prj-1")

    assert [call.args[0] for call in runtime.get.call_args_list] == [
        "ntmprj-1.Sat0",
        "ntmprj-1.Sat1",
        "ntmprj-1.Gnd0",
    ]
    # A failed lookup leaves previously attached state untouched.
    assert nodes[0].container == "stale"
    assert nodes[0].container_pid == 99
    assert nodes[1].container is not None
    assert nodes[1].container_pid is None
    assert nodes[2].container_pid == 4321
    assert all(node.grpc_client is launcher._client for node in nodes)
    assert all(node.project_id == "prj-1" for node in nodes)
    assert "could not attach container for Sat0: not found" in caplog.text
    assert "attached 2/3 containers" in caplog.text


# close_project


def test_close_project_reports_progress_and_closes_channel():
    launcher = _new_launcher()
    launcher._client = MagicMock()
    request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._request = request
    launcher._client.ProjectClose.return_value = [
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_COUNT, total=2),
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_CLOSE),
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.BRIDGE_COUNT, total=1),
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_CLOSE),
    ]
    channel = launcher._channel = MagicMock()
    events = []

    launcher.close_project(progress_cb=lambda *event: events.append(event))

    assert events == [
        ("CLOSE_NODE_COUNT", 0, 2),
        ("CLOSE_NODE_CLOSE", 1, 2),
        ("CLOSE_NODE_CLOSE", 2, 2),
        ("CLOSE_COMPLETED", 2, 2),
    ]
    channel.close.assert_called_once()
    assert launcher._client is None
    assert launcher._channel is None


def test_close_project_counts_batched_node_close_totals():
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.ProjectClose.return_value = [
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_COUNT, total=5),
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_CLOSE, total=3),
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_CLOSE, total=2),
    ]
    launcher._channel = MagicMock()

    launcher.close_project()


def test_close_project_rejects_partial_node_close():
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.ProjectClose.return_value = [
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_COUNT, total=2),
        netem_pb2.ProjectCloseMsg(code=netem_pb2.ProjectCloseMsg.NODE_CLOSE),
    ]
    launcher._channel = MagicMock()

    with pytest.raises(RuntimeError, match="incomplete"):
        launcher.close_project()


def test_close_project_reports_error_and_completion_then_reraises(caplog):
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.ProjectClose.side_effect = RuntimeError("stream broken")
    channel = launcher._channel = MagicMock()
    events = []

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="stream broken"):
            launcher.close_project(progress_cb=lambda *event: events.append(event))

    assert events == [("CLOSE_ERROR", 0, 0), ("CLOSE_COMPLETED", 0, 0)]
    assert "ProjectClose failed: stream broken" in caplog.text
    channel.close.assert_called_once()
    assert launcher._client is None
    assert launcher._channel is None


def test_close_project_returns_without_side_effects_before_project_open():
    launcher = _new_launcher()
    launcher._client = None
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    channel = launcher._channel = MagicMock()
    progress = MagicMock()

    launcher.close_project(progress)

    launcher._client = MagicMock()
    launcher._request = None
    launcher.close_project(progress)

    progress.assert_not_called()
    channel.close.assert_not_called()


# Per-link operations


def test_update_link_raises_before_project_open():
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = None

    with pytest.raises(RuntimeError, match="update_link called before project is open"):
        launcher.update_link(_link())

    launcher._client.LinkUpdate.assert_not_called()


def test_update_link_sends_link_request_for_open_project():
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.LinkUpdate.return_value = _ack()

    launcher.update_link(_link())

    request = launcher._client.LinkUpdate.call_args.args[0]
    assert request.prjId == "prj-1"
    assert request.link.peer1 == "Sat0.1"
    assert request.link.peer2 == "Sat1.0"


def test_update_link_raises_runtime_error_on_rpc_failure():
    cause = RuntimeError("rpc failed")
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.LinkUpdate.side_effect = cause

    with pytest.raises(RuntimeError) as excinfo:
        launcher.update_link(_link())

    assert str(excinfo.value) == (
        "GoNetEmLauncher: LinkUpdate failed for Sat0<->Sat1: rpc failed"
    )
    assert excinfo.value.__cause__ is cause


@pytest.mark.parametrize(
    ("method_name", "rpc_name", "action"),
    [
        ("add_link", "LinkAdd", "LinkAdd"),
        ("delete_link", "LinkDel", "LinkDel"),
        ("update_link", "LinkUpdate", "LinkUpdate"),
    ],
)
def test_link_mutation_raises_on_error_ack(method_name, rpc_name, action):
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    getattr(launcher._client, rpc_name).return_value = _ack(
        netem_pb2.StatusCode.ERROR, "backend refused"
    )

    with pytest.raises(RuntimeError, match=f"{action} failed"):
        getattr(launcher, method_name)(_link())


@pytest.mark.parametrize(
    ("method_name", "rpc_name", "action"),
    [
        ("add_link", "LinkAdd", "LinkAdd"),
        ("delete_link", "LinkDel", "LinkDel"),
        ("update_link", "LinkUpdate", "LinkUpdate"),
    ],
)
def test_link_mutation_raises_when_ack_has_no_status(method_name, rpc_name, action):
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    getattr(launcher._client, rpc_name).return_value = SimpleNamespace()

    with pytest.raises(RuntimeError, match=f"{action} failed"):
        getattr(launcher, method_name)(_link())


_LINK_MUTATIONS = [("add_link", "LinkAdd"), ("delete_link", "LinkDel")]


@pytest.mark.parametrize("method_name", ["add_link", "delete_link"])
def test_link_mutation_raises_before_project_open(method_name):
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = None

    with pytest.raises(
        RuntimeError, match=f"{method_name} called before project is open"
    ):
        getattr(launcher, method_name)(_link())

    rpc_name = dict(_LINK_MUTATIONS)[method_name]
    getattr(launcher._client, rpc_name).assert_not_called()


@pytest.mark.parametrize(("method_name", "rpc_name"), _LINK_MUTATIONS)
def test_link_mutation_forwards_link_request(method_name, rpc_name):
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    getattr(launcher._client, rpc_name).return_value = _ack()

    getattr(launcher, method_name)(_link())

    request = getattr(launcher._client, rpc_name).call_args.args[0]
    assert request.prjId == "prj-1"
    assert request.link.peer1 == "Sat0.1"
    assert request.link.peer2 == "Sat1.0"


def test_add_link_raises_runtime_error_on_rpc_failure():
    cause = RuntimeError("rpc failed")
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.LinkAdd.side_effect = cause

    with pytest.raises(RuntimeError) as excinfo:
        launcher.add_link(_link())

    assert str(excinfo.value) == (
        "GoNetEmLauncher: LinkAdd failed for Sat0<->Sat1: rpc failed"
    )
    assert excinfo.value.__cause__ is cause


def test_delete_link_raises_runtime_error_on_rpc_failure():
    cause = RuntimeError("rpc failed")
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.LinkDel.side_effect = cause

    with pytest.raises(RuntimeError) as excinfo:
        launcher.delete_link(_link())

    assert str(excinfo.value) == (
        "GoNetEmLauncher: LinkDel failed for Sat0<->Sat1: rpc failed"
    )
    assert excinfo.value.__cause__ is cause


def test_update_links_propagates_rpc_error_unchanged():
    error = grpc.RpcError("unimplemented")
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.LinkUpdateMany.side_effect = error

    with pytest.raises(grpc.RpcError) as excinfo:
        launcher.update_links([_link()])

    assert excinfo.value is error


def test_update_links_wraps_generic_failure_with_chunk_size():
    cause = RuntimeError("transport reset")
    launcher = _new_launcher()
    launcher._client = MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    launcher._client.LinkUpdateMany.side_effect = cause

    with pytest.raises(RuntimeError) as excinfo:
        launcher.update_links([_link(), _link()])

    assert str(excinfo.value) == (
        "GoNetEmLauncher: LinkUpdateMany failed for 2 link(s): transport reset"
    )
    assert excinfo.value.__cause__ is cause


def test_start_containers_deletes_project_file_on_failure(grpc_client, tmp_path):
    project_file = tmp_path / "project.gnet"
    project_file.write_bytes(b"project")
    grpc_client.TopologyRun.side_effect = RuntimeError("topology exploded")
    launcher, connection, _factory = _start_launcher(tmp_path, grpc_client)
    launcher._command_runner = MagicMock()
    launcher._command_runner.run.return_value = SimpleNamespace(
        returncode=0, stdout="", stderr=""
    )
    with patch.object(launcher, "generate_project", return_value=str(project_file)):
        with pytest.raises(RuntimeError, match="TopologyRun failed"):
            launcher.start_containers([])

    assert not project_file.exists()
    connection.close.assert_called_once()
    assert launcher._client is None
    assert launcher._request is None


# TopologyRun completion / count validation


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (
            [
                _run_msg(netem_pb2.TopologyRunMsg.NODE_COUNT, 2),
                _run_msg(netem_pb2.TopologyRunMsg.NODE_START),
            ],
            r"started 1 of 2 node",
        ),
        (
            [
                _run_msg(netem_pb2.TopologyRunMsg.LINK_COUNT, 2),
                _run_msg(netem_pb2.TopologyRunMsg.LINK_SETUP),
            ],
            r"set up 1 of 2 link",
        ),
        (
            [
                _run_msg(netem_pb2.TopologyRunMsg.NODE_COUNT, 1),
                _run_msg(netem_pb2.TopologyRunMsg.NODE_START),
                _run_msg(netem_pb2.TopologyRunMsg.NODE_START),
            ],
            r"started 2 of 1 node",
        ),
    ],
)
def test_start_containers_rejects_partial_topology_run(
    grpc_client, tmp_path, stream, message
):
    grpc_client.TopologyRun.return_value = stream
    launcher, connection, _factory = _start_launcher(tmp_path, grpc_client)
    launcher.force_close_project = MagicMock(return_value=True)

    with pytest.raises(RuntimeError, match=message):
        launcher.start_containers([])

    launcher.force_close_project.assert_called_once_with()
    connection.close.assert_called_once()
    assert launcher._request is None
    assert launcher._client is None


def test_start_containers_accepts_a_complete_stream_with_no_counts(
    grpc_client, tmp_path
):
    grpc_client.TopologyRun.return_value = []
    launcher, _connection, _factory = _start_launcher(tmp_path, grpc_client)

    assert launcher.start_containers([]) == (0.0, 0.0)
