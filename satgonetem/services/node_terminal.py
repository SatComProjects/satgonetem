"""Node terminal client for the GoNetEm NodeExec stream."""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class NodeTerminal:
    """Interactive bash terminal on a GoNetEm node via the NodeExec bidirectional stream."""

    def __init__(self, client: Any, project_id: str, node_name: str) -> None:
        self._client = client
        self._project_id = project_id
        self._node_name = node_name
        self._input_queue: queue.Queue = queue.Queue()
        self._response_queue: queue.Queue = queue.Queue()
        self._stream: Any = None
        self._recv_thread: threading.Thread | None = None
        self._closed = False
        self._recv_error: BaseException | None = None

    @property
    def closed(self) -> bool:
        """Return True once the stream ended or the terminal was closed."""
        return self._closed

    @property
    def receive_error(self) -> BaseException | None:
        """Return the exception that terminated the receive stream, if any."""
        return self._recv_error

    def _request_generator(self):
        from satgonetem.proto import netem_pb2

        yield netem_pb2.ExecCltMsg(
            code=netem_pb2.ExecCltMsg.CMD,
            prjId=self._project_id,
            node=self._node_name,
            cmd=["/bin/bash"],
            tty=True,
        )

        while True:
            msg = self._input_queue.get()
            if msg is None:
                break
            yield msg

    def _recv_loop(self) -> None:
        try:
            for response in self._stream:
                self._response_queue.put(response)
        except Exception as exc:
            if self._closed:
                # Cancelling the stream during close() is expected; do not
                # report it as a server-side failure.
                logging.debug(
                    "NodeTerminal receive stream for %s ended during close: %s",
                    self._node_name,
                    exc,
                )
            else:
                self._recv_error = exc
                logging.error(
                    "NodeTerminal receive stream failed for %s: %s",
                    self._node_name,
                    exc,
                )
        finally:
            # A server-side EOF ends the session just like close() does: later
            # sends must fail instead of growing the unbounded input queue.
            self._closed = True
            self._input_queue.put(None)
            self._response_queue.put(None)

    def __enter__(self) -> NodeTerminal:
        if self._closed:
            raise RuntimeError("Terminal is closed")
        self._stream = self._client.NodeExec(self._request_generator())
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def send(self, data: bytes | str) -> None:
        """Send data to the terminal's stdin."""
        if self._closed:
            raise RuntimeError("Terminal is closed")
        from satgonetem.proto import netem_pb2

        if isinstance(data, str):
            data = data.encode("utf-8")
        msg = netem_pb2.ExecCltMsg(
            code=netem_pb2.ExecCltMsg.DATA,
            prjId=self._project_id,
            node=self._node_name,
            data=data,
        )
        self._input_queue.put(msg)

    def resize(self, width: int, height: int) -> None:
        """Resize the TTY."""
        if self._closed:
            raise RuntimeError("Terminal is closed")
        from satgonetem.proto import netem_pb2

        msg = netem_pb2.ExecCltMsg(
            code=netem_pb2.ExecCltMsg.RESIZE,
            prjId=self._project_id,
            node=self._node_name,
            ttyWidth=width,
            ttyHeight=height,
        )
        self._input_queue.put(msg)

    def close(self) -> None:
        """Close the terminal session and cancel the underlying stream."""
        if self._closed:
            return
        self._closed = True
        from satgonetem.proto import netem_pb2

        msg = netem_pb2.ExecCltMsg(
            code=netem_pb2.ExecCltMsg.CLOSE,
            prjId=self._project_id,
            node=self._node_name,
        )
        self._input_queue.put(msg)
        self._input_queue.put(None)

        cancel = getattr(self._stream, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as exc:
                logging.warning(
                    "Failed to cancel NodeTerminal stream for %s: %s",
                    self._node_name,
                    exc,
                )

        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2)

    def read(self, timeout: float | None = None) -> tuple[str, bytes] | None:
        """Read the next response from the terminal.

        Args:
            timeout: Seconds to wait for a response. None means block forever.

        Returns:
            A tuple of (code, data) where code is 'stdout', 'stderr', 'error',
            or 'close'. Returns None when the stream has ended.
        """
        from satgonetem.proto import netem_pb2

        response = self._response_queue.get(timeout=timeout)
        if response is None:
            self._response_queue.put(None)
            return None
        code_map = {
            netem_pb2.ExecSrvMsg.STDOUT: "stdout",
            netem_pb2.ExecSrvMsg.STDERR: "stderr",
            netem_pb2.ExecSrvMsg.ERROR: "error",
            netem_pb2.ExecSrvMsg.CLOSE: "close",
        }
        return code_map.get(response.code, "unknown"), response.data
