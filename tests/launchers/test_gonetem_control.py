"""Behavioral tests for the optional GoNetEm control RPCs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import grpc
import pytest

from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
from satgonetem.proto import netem_pb2

pytestmark = pytest.mark.unit


def _new_launcher(**kwargs) -> GoNetEmLauncher:
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


def _open_launcher(client=None) -> GoNetEmLauncher:
    launcher = _new_launcher()
    launcher._client = client or MagicMock()
    launcher._request = netem_pb2.ProjectRequest(id="prj-1")
    return launcher


def _ack(code=netem_pb2.StatusCode.OK, error="") -> netem_pb2.AckResponse:
    return netem_pb2.AckResponse(status=netem_pb2.Status(code=code, error=error))


def _link():
    return SimpleNamespace(
        source=SimpleNamespace(id=0, name="Sat0"),
        target=SimpleNamespace(id=1, name="Sat1"),
        delay=10,
        loss=0.0,
        jitter=0,
        type="InterSatelliteLink",
        peer1_capacity=1000,
        peer2_capacity=1000,
    )


# Preconditions


_UNARY_CONTROL_CALLS = [
    ("restart_node", ("Sat0",)),
    ("set_interface_state", ("Sat0", 1, True)),
    ("stop_all_nodes", ()),
    ("start_all_nodes", ()),
    ("get_backend_status", ()),
]


@pytest.mark.parametrize(("method_name", "args"), _UNARY_CONTROL_CALLS)
def test_control_requires_an_open_project(method_name, args):
    launcher = _new_launcher()
    launcher._client = None
    launcher._request = None

    with pytest.raises(RuntimeError, match="requires an open project"):
        getattr(launcher, method_name)(*args)


def test_capture_requires_an_open_project():
    launcher = _new_launcher()
    launcher._client = None
    launcher._request = None

    with pytest.raises(RuntimeError, match="requires an open project"):
        launcher.capture_interface("Sat0", 1)


def test_save_project_requires_an_open_project():
    launcher = _new_launcher()
    launcher._client = None
    launcher._request = None

    with pytest.raises(RuntimeError, match="requires an open project"):
        launcher.save_project()


# Unary control RPCs


def test_restart_node_sends_project_and_node_names():
    launcher = _open_launcher()
    launcher._client.NodeRestart.return_value = _ack()

    launcher.restart_node("Sat0")

    request = launcher._client.NodeRestart.call_args.args[0]
    assert request.prjId == "prj-1"
    assert request.node == "Sat0"


def test_restart_node_raises_with_application_error():
    launcher = _open_launcher()
    launcher._client.NodeRestart.return_value = _ack(
        netem_pb2.StatusCode.ERROR, "container restart failed"
    )

    with pytest.raises(RuntimeError, match="container restart failed"):
        launcher.restart_node("Sat0")


def test_set_interface_state_sends_if_index_and_state():
    launcher = _open_launcher()
    launcher._client.NodeSetIfState.return_value = _ack()

    launcher.set_interface_state("Sat0", 3, False)

    request = launcher._client.NodeSetIfState.call_args.args[0]
    assert request.prjId == "prj-1"
    assert request.node == "Sat0"
    assert request.ifIndex == 3
    assert request.state == netem_pb2.IfState.DOWN


def test_set_interface_state_up_uses_up_enum():
    launcher = _open_launcher()
    launcher._client.NodeSetIfState.return_value = _ack()

    launcher.set_interface_state("Gnd0", 0, True)

    request = launcher._client.NodeSetIfState.call_args.args[0]
    assert request.ifIndex == 0
    assert request.state == netem_pb2.IfState.UP


def test_set_interface_state_raises_with_application_error():
    launcher = _open_launcher()
    launcher._client.NodeSetIfState.return_value = _ack(
        netem_pb2.StatusCode.ERROR, "interface not found"
    )

    with pytest.raises(RuntimeError, match="interface not found"):
        launcher.set_interface_state("Sat0", 9, True)


@pytest.mark.parametrize(
    ("method_name", "rpc_name"),
    [("stop_all_nodes", "TopologyStopAll"), ("start_all_nodes", "TopologyStartAll")],
)
def test_bulk_node_control_sends_project_request(method_name, rpc_name):
    launcher = _open_launcher()
    getattr(launcher._client, rpc_name).return_value = _ack()

    getattr(launcher, method_name)()

    request = getattr(launcher._client, rpc_name).call_args.args[0]
    assert request.id == "prj-1"


@pytest.mark.parametrize(
    ("method_name", "rpc_name"),
    [("stop_all_nodes", "TopologyStopAll"), ("start_all_nodes", "TopologyStartAll")],
)
def test_bulk_node_control_raises_with_application_error(method_name, rpc_name):
    launcher = _open_launcher()
    getattr(launcher._client, rpc_name).return_value = _ack(
        netem_pb2.StatusCode.ERROR, "topology is busy"
    )

    with pytest.raises(RuntimeError, match="topology is busy"):
        getattr(launcher, method_name)()


def test_control_wraps_grpc_error_with_context():
    launcher = _open_launcher()
    cause = grpc.RpcError("transport down")
    launcher._client.NodeRestart.side_effect = cause

    with pytest.raises(RuntimeError, match="NodeRestart") as excinfo:
        launcher.restart_node("Sat0")

    assert excinfo.value.__cause__ is cause


# Capture streaming


def test_capture_interface_concatenates_stdout_and_ignores_other_stream_codes():
    launcher = _open_launcher()
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=b"pcap-"),
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDERR, data=b"warn"),
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=b"body"),
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.OK),
    ]

    captured = launcher.capture_interface("Sat0", 1, timeout_s=5.5)

    assert captured == b"pcap-body"
    request = launcher._client.NodeCapture.call_args.args[0]
    assert request.prjId == "prj-1"
    assert request.node == "Sat0"
    assert request.ifIndex == 1
    assert launcher._client.NodeCapture.call_args.kwargs["timeout"] == 5.5


def test_capture_interface_returns_partial_data_on_deadline():
    launcher = _open_launcher()

    class _Deadline(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.DEADLINE_EXCEEDED

    def stream(*args, **kwargs):
        yield netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=b"pcap")
        raise _Deadline()

    launcher._client.NodeCapture.side_effect = stream

    assert launcher.capture_interface("Sat0", 1, timeout_s=0.1) == b"pcap"


def test_capture_interface_raises_for_empty_clean_stream():
    launcher = _open_launcher()
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDERR, data=b"warn"),
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.OK),
    ]

    with pytest.raises(RuntimeError, match="no capture data"):
        launcher.capture_interface("Sat0", 1)


def test_capture_interface_raises_for_truncated_clean_stream():
    launcher = _open_launcher()
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=b"pcap"),
    ]

    with pytest.raises(RuntimeError, match="without a completion marker"):
        launcher.capture_interface("Sat0", 1)


def test_capture_interface_accepts_complete_pcap_without_ok_marker():
    launcher = _open_launcher()
    pcap = (
        b"\xd4\xc3\xb2\xa1"
        + b"\x00" * 20
        + b"\x00\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + b"\x04\x00\x00\x00"
        + b"\x04\x00\x00\x00"
        + b"\x00" * 4
    )
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=pcap),
    ]

    assert launcher.capture_interface("Sat0", 1) == pcap


def test_capture_interface_rejects_header_only_clean_stream():
    launcher = _open_launcher()
    pcap = b"\xd4\xc3\xb2\xa1" + b"\x00" * 20
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=pcap),
    ]

    with pytest.raises(RuntimeError, match="without a completion marker"):
        launcher.capture_interface("Sat0", 1)


def test_capture_interface_rejects_partial_record_clean_stream():
    launcher = _open_launcher()
    pcap = (
        b"\xd4\xc3\xb2\xa1"
        + b"\x00" * 20
        + b"\x00" * 16
        + b"\x08\x00\x00\x00"
        + b"\x00" * 2
    )
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=pcap),
    ]

    with pytest.raises(RuntimeError, match="without a completion marker"):
        launcher.capture_interface("Sat0", 1)


def test_capture_interface_raises_on_error_message():
    launcher = _open_launcher()
    launcher._client.NodeCapture.return_value = [
        netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=b"pcap"),
        netem_pb2.CaptureSrvMsg(
            code=netem_pb2.CaptureSrvMsg.ERROR, data=b"tcpdump failed"
        ),
    ]

    with pytest.raises(RuntimeError, match="tcpdump failed"):
        launcher.capture_interface("Sat0", 1)


def test_capture_interface_wraps_grpc_error():
    launcher = _open_launcher()
    cause = grpc.RpcError("stream broken")
    launcher._client.NodeCapture.side_effect = cause

    with pytest.raises(RuntimeError, match="NodeCapture") as excinfo:
        launcher.capture_interface("Sat0", 1)

    assert excinfo.value.__cause__ is cause


# Save project stream


def test_save_project_concatenates_only_data_messages():
    launcher = _open_launcher()
    launcher._client.ProjectSave.return_value = [
        netem_pb2.ProjectSaveMsg(code=netem_pb2.ProjectSaveMsg.NODE_COUNT, total=2),
        netem_pb2.ProjectSaveMsg(code=netem_pb2.ProjectSaveMsg.DATA, data=b"arc"),
        netem_pb2.ProjectSaveMsg(code=netem_pb2.ProjectSaveMsg.NODE_SAVE),
        netem_pb2.ProjectSaveMsg(code=netem_pb2.ProjectSaveMsg.DATA, data=b"hive"),
    ]

    saved = launcher.save_project(timeout_s=12.0)

    assert saved == b"archive"
    assert launcher._client.ProjectSave.call_args.kwargs["timeout"] == 12.0


def test_save_project_rejects_empty_output():
    launcher = _open_launcher()
    launcher._client.ProjectSave.return_value = [
        netem_pb2.ProjectSaveMsg(code=netem_pb2.ProjectSaveMsg.NODE_COUNT, total=1),
    ]

    with pytest.raises(RuntimeError, match="no project data"):
        launcher.save_project()


def test_save_project_raises_with_application_error():
    launcher = _open_launcher()
    launcher._client.ProjectSave.return_value = [
        netem_pb2.ProjectSaveMsg(code=netem_pb2.ProjectSaveMsg.DATA, data=b"arc"),
    ]
    # ProjectSave carries no Status field; transport failures are the only
    # application-level error signal for this RPC.
    cause = grpc.RpcError("save aborted")
    launcher._client.ProjectSave.side_effect = cause

    with pytest.raises(RuntimeError, match="ProjectSave") as excinfo:
        launcher.save_project()

    assert excinfo.value.__cause__ is cause


# Backend status


def test_get_backend_status_returns_json_serializable_dict():
    launcher = _open_launcher()
    response = netem_pb2.StatusResponse(
        status=netem_pb2.Status(code=netem_pb2.StatusCode.OK),
        name="project",
        id="prj-1",
        openAt="2026-11-22T10:00:00Z",
        running=True,
    )
    node = response.nodes.add(name="Sat0", running=True)
    node.interfaces.add(name="eth0", state=netem_pb2.IfState.UP)
    node.interfaces.add(name="eth1", state=netem_pb2.IfState.DOWN)
    launcher._client.ProjectGetStatus.return_value = response

    status = launcher.get_backend_status()

    assert status == {
        "name": "project",
        "id": "prj-1",
        "open_at": "2026-11-22T10:00:00Z",
        "running": True,
        "nodes": [
            {
                "name": "Sat0",
                "running": True,
                "interfaces": [
                    {"name": "eth0", "up": True},
                    {"name": "eth1", "up": False},
                ],
            }
        ],
    }
    json.dumps(status)


def test_get_backend_status_raises_with_application_error():
    launcher = _open_launcher()
    launcher._client.ProjectGetStatus.return_value = netem_pb2.StatusResponse(
        status=netem_pb2.Status(
            code=netem_pb2.StatusCode.ERROR, error="project not found"
        )
    )

    with pytest.raises(RuntimeError, match="project not found"):
        launcher.get_backend_status()


# Link request impairment fields


def test_link_request_forwards_loss_and_jitter_to_both_peers():
    launcher = _open_launcher()
    link = _link()
    link.loss = 2.5
    link.jitter = 15

    request = launcher._link_request(link)

    assert request.link.peer1qos.loss == pytest.approx(2.5)
    assert request.link.peer2qos.loss == pytest.approx(2.5)
    assert request.link.peer1qos.jitter == 15
    assert request.link.peer2qos.jitter == 15


def test_link_request_defaults_to_zero_impairments():
    launcher = _open_launcher()

    request = launcher._link_request(_link())

    assert request.link.peer1qos.loss == 0.0
    assert request.link.peer2qos.loss == 0.0
    assert request.link.peer1qos.jitter == 0
    assert request.link.peer2qos.jitter == 0


def test_link_capacities_preserves_zero_for_unclosable_budget_link():
    launcher = _open_launcher()
    link = _link()
    link.use_budget = True
    link.peer1_capacity = 0
    link.peer2_capacity = 500

    assert launcher._link_capacities_for(link) == (0, 500)


def test_capture_interface_raises_on_deadline_with_only_empty_stdout_chunks():
    launcher = _open_launcher()

    class _Deadline(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.DEADLINE_EXCEEDED

    def stream(*args, **kwargs):
        yield netem_pb2.CaptureSrvMsg(code=netem_pb2.CaptureSrvMsg.STDOUT, data=b"")
        raise _Deadline()

    launcher._client.NodeCapture.side_effect = stream

    with pytest.raises(RuntimeError, match="NodeCapture.*failed"):
        launcher.capture_interface("Sat0", 1, timeout_s=0.1)
