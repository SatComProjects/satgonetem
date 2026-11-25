"""Optional GoNetEm control RPCs for an open project.

``NetworkLauncher`` does not declare these operations as abstract methods:
they are backend capabilities, feature-detected at the call site by
``BackendControlMixin``. Keeping them optional means a launcher that only
implements the base lifecycle contract keeps working unchanged.
"""

from __future__ import annotations

import struct
from typing import Any

import grpc

from satgonetem.proto import netem_pb2

# Global-header prefixes for the capture formats tcpdump can emit. A clean
# stream EOF without an explicit OK marker is only accepted when the payload
# still carries a complete-enough header for one of these formats.
_PCAP_MAGICS = frozenset(
    {
        b"\xa1\xb2\xc3\xd4",
        b"\xd4\xc3\xb2\xa1",
        b"\xa1\xb2\x3c\x4d",
        b"\x4d\x3c\xb2\xa1",
    }
)
_PCAP_MIN_LEN = 24
_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"
_PCAPNG_MIN_LEN = 28


def _pcap_is_complete(data: bytes) -> bool:
    """Return True when *data* is a complete classic pcap file.

    A clean stream without an ``OK`` marker is accepted only when every
    record header and body is present. Header-only or partially written
    records are rejected instead of being saved as a successful capture.
    """
    if len(data) < _PCAP_MIN_LEN or data[:4] not in _PCAP_MAGICS:
        return False
    if data[:4] == _PCAPNG_MAGIC:
        # pcapng block validation is not implemented; require the OK marker.
        return False
    endian = "<" if data[:4] in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"} else ">"
    offset = _PCAP_MIN_LEN
    records = 0
    while offset < len(data):
        if offset + 16 > len(data):
            return False
        incl_len = struct.unpack_from(f"{endian}I", data, offset + 8)[0]
        offset += 16
        if offset + incl_len > len(data):
            return False
        offset += incl_len
        records += 1
    return records > 0 and offset == len(data)


class GoNetEmControlMixin:
    """Node lifecycle, interface state, capture, and snapshot RPCs."""

    _client: Any
    _request: Any

    def _control_context(self, action: str) -> tuple[Any, Any]:
        """Return the open client and project request.

        Raises:
            RuntimeError: If no project has been opened yet.
        """
        if self._client is None or self._request is None:
            raise RuntimeError(
                f"GoNetEmLauncher: {action} requires an open project; "
                "call start_containers() first"
            )
        return self._client, self._request

    @staticmethod
    def _validate_if_index(if_index: int) -> int:
        if isinstance(if_index, bool) or not isinstance(if_index, int):
            raise TypeError(
                f"GoNetEmLauncher: if_index must be an int, got {if_index!r}"
            )
        if if_index < 0:
            raise ValueError(
                f"GoNetEmLauncher: if_index must be non-negative, got {if_index}"
            )
        return if_index

    @staticmethod
    def _validate_timeout(timeout_s: float) -> float:
        if timeout_s <= 0:
            raise ValueError(
                f"GoNetEmLauncher: timeout_s must be positive, got {timeout_s}"
            )
        return float(timeout_s)

    @staticmethod
    def _require_ok(status: Any, action: str) -> None:
        """Raise ``RuntimeError`` when *status* is not ``OK``."""
        if status.code != netem_pb2.StatusCode.OK:
            detail = getattr(status, "error", "") or "server returned a non-OK status"
            raise RuntimeError(f"GoNetEmLauncher: {action} failed: {detail}")

    def _unary_control(self, action: str, rpc_name: str, request: Any) -> Any:
        """Call a unary control RPC, wrapping transport failures."""
        client, _request = self._control_context(action)
        try:
            response = getattr(client, rpc_name)(request)
        except grpc.RpcError as exc:
            raise RuntimeError(f"GoNetEmLauncher: {action} failed: {exc}") from exc
        self._require_ok(response.status, action)
        return response

    def restart_node(self, node_name: str) -> None:
        """Restart one node container.

        The caller must reconcile node state afterwards: addresses, routes,
        qdiscs, and interface state do not survive a container restart.
        """
        action = f"NodeRestart for {node_name}"
        _client, request = self._control_context(action)
        self._unary_control(
            action,
            "NodeRestart",
            netem_pb2.NodeRequest(prjId=request.id, node=node_name),
        )

    def set_interface_state(self, node_name: str, if_index: int, up: bool) -> None:
        """Bring interface ``if_index`` of *node_name* up or down."""
        if not isinstance(up, bool):
            raise TypeError(f"GoNetEmLauncher: up must be a bool, got {up!r}")
        if_index = self._validate_if_index(if_index)
        action = f"NodeSetIfState for {node_name} ifIndex={if_index}"
        _client, request = self._control_context(action)
        self._unary_control(
            action,
            "NodeSetIfState",
            netem_pb2.NodeIfStateRequest(
                prjId=request.id,
                node=node_name,
                ifIndex=if_index,
                state=netem_pb2.IfState.UP if up else netem_pb2.IfState.DOWN,
            ),
        )

    def stop_all_nodes(self) -> None:
        """Stop every node container in the open project."""
        action = "TopologyStopAll"
        _client, request = self._control_context(action)
        self._unary_control(action, "TopologyStopAll", request)

    def start_all_nodes(self) -> None:
        """Start every node container in the open project.

        Addresses, routes, and qdiscs must be reapplied by the caller after
        the containers come back.
        """
        action = "TopologyStartAll"
        _client, request = self._control_context(action)
        self._unary_control(action, "TopologyStartAll", request)

    def capture_interface(
        self, node_name: str, if_index: int, timeout_s: float = 30.0
    ) -> bytes:
        """Capture traffic on one interface and return the capture bytes.

        Framing assumption: ``CaptureSrvMsg`` has no DATA code. The server
        sends capture payload on ``STDOUT``, human-readable diagnostics on
        ``STDERR``, and uses ``OK`` as the completion marker. ``STDOUT``
        chunks are concatenated into a single pcap byte string. An ``ERROR``
        message aborts the capture.

        A capture is only returned when the server completed the stream
        (``OK``) or the accumulated bytes form a valid pcap/pcapng prefix; an
        empty or truncated clean EOF raises. A deadline still returns the
        non-empty bytes captured so far.

        Args:
            node_name: Node whose interface is captured.
            if_index: Interface index (``eth<digits>`` suffix).
            timeout_s: Deadline for the streaming call, in seconds.
        """
        if_index = self._validate_if_index(if_index)
        timeout_s = self._validate_timeout(timeout_s)
        action = f"NodeCapture for {node_name} ifIndex={if_index}"
        client, request = self._control_context(action)
        capture_request = netem_pb2.NodeInterfaceRequest(
            prjId=request.id, node=node_name, ifIndex=if_index
        )
        chunks: list[bytes] = []
        completed = False
        try:
            for msg in client.NodeCapture(capture_request, timeout=timeout_s):
                if msg.code == netem_pb2.CaptureSrvMsg.STDOUT:
                    if msg.data:
                        chunks.append(msg.data)
                elif msg.code == netem_pb2.CaptureSrvMsg.OK:
                    completed = True
                elif msg.code == netem_pb2.CaptureSrvMsg.ERROR:
                    detail = msg.data.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(
                        f"GoNetEmLauncher: {action} failed: "
                        f"{detail or 'server reported ERROR'}"
                    )
                # STDERR is diagnostic only.
        except grpc.RpcError as exc:
            # NodeCapture is a live stream: the server keeps capturing until
            # the client stops or the deadline expires. Return the valid pcap
            # prefix produced so far instead of discarding a bounded capture.
            if (
                getattr(exc, "code", lambda: None)()
                == grpc.StatusCode.DEADLINE_EXCEEDED
                and chunks
            ):
                return b"".join(chunks)
            raise RuntimeError(f"GoNetEmLauncher: {action} failed: {exc}") from exc

        data = b"".join(chunks)
        if not data:
            raise RuntimeError(
                f"GoNetEmLauncher: {action} failed: server returned no capture data"
            )
        if not completed and not _pcap_is_complete(data):
            raise RuntimeError(
                f"GoNetEmLauncher: {action} failed: capture stream ended "
                "without a completion marker or a complete pcap file"
            )
        return data

    def get_backend_status(self) -> dict:
        """Return a JSON-serializable snapshot of the open project status."""
        action = "ProjectGetStatus"
        _client, request = self._control_context(action)
        response = self._unary_control(action, action, request)
        return {
            "name": response.name,
            "id": response.id,
            "open_at": response.openAt,
            "running": response.running,
            "nodes": [
                {
                    "name": node.name,
                    "running": node.running,
                    "interfaces": [
                        {
                            "name": interface.name,
                            "up": interface.state == netem_pb2.IfState.UP,
                        }
                        for interface in node.interfaces
                    ],
                }
                for node in response.nodes
            ],
        }

    def save_project(self, timeout_s: float = 60.0) -> bytes:
        """Save the open project archive and return its bytes.

        Only ``ProjectSaveMsg.DATA`` messages carry the archive; progress
        messages (``NODE_COUNT``/``NODE_SAVE``) are ignored. An empty archive
        is rejected because it would silently produce an unusable snapshot.

        Args:
            timeout_s: Deadline for the streaming call, in seconds.
        """
        timeout_s = self._validate_timeout(timeout_s)
        action = "ProjectSave"
        client, request = self._control_context(action)
        chunks: list[bytes] = []
        try:
            for msg in client.ProjectSave(request, timeout=timeout_s):
                if msg.code == netem_pb2.ProjectSaveMsg.DATA:
                    chunks.append(msg.data)
        except grpc.RpcError as exc:
            raise RuntimeError(f"GoNetEmLauncher: {action} failed: {exc}") from exc
        data = b"".join(chunks)
        if not data:
            raise RuntimeError("GoNetEmLauncher: ProjectSave returned no project data")
        return data
