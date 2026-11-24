"""Unit tests for the CLI node terminal bridge."""

from __future__ import annotations

import fcntl
import queue
import select
import signal
import struct
import sys
import termios
import tty
from types import SimpleNamespace
from unittest.mock import MagicMock

import grpc
import pytest

from satgonetem.cli import node_terminal_bridge
from satgonetem.proto import netem_pb2, netem_pb2_grpc

pytestmark = pytest.mark.unit


def test_sigwinch_handler_defers_resize_to_main_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGWINCH must not call input_queue.put() re-entrantly from signal context."""
    captured_handlers: dict[int, object] = {}
    in_signal_handler = False
    put_called_in_signal_handler = False
    ioctl_called_in_signal_handler = False

    real_queue_cls = queue.Queue

    class _TrackingQueue(real_queue_cls):
        def put(self, item, block=True, timeout=None):
            nonlocal put_called_in_signal_handler
            if in_signal_handler:
                put_called_in_signal_handler = True
            return super().put(item, block=block, timeout=timeout)

    monkeypatch.setattr(node_terminal_bridge.queue, "Queue", _TrackingQueue)

    def fake_signal(sig, handler):
        captured_handlers[sig] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    def fake_ioctl(_fd, _req, _buf):
        nonlocal ioctl_called_in_signal_handler
        if in_signal_handler:
            ioctl_called_in_signal_handler = True
        return struct.pack("HHHH", 40, 120, 0, 0)

    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(termios, "tcgetattr", lambda _fd: [0])
    monkeypatch.setattr(termios, "tcsetattr", lambda _fd, _when, _attrs: None)
    monkeypatch.setattr(tty, "setraw", lambda _fd: None)

    fake_stdin = SimpleNamespace(fileno=lambda: 0)
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    sent_messages: list[netem_pb2.ExecCltMsg] = []
    select_calls = 0

    def fake_node_exec(req_iter):
        # Drain requests in a background-like generator? Instead, return a
        # stream that blocks until select has run twice, then closes while
        # draining sent_messages.
        def _responses():
            while select_calls < 2:
                pass
            yield netem_pb2.ExecSrvMsg(code=netem_pb2.ExecSrvMsg.CLOSE)

        # Keep a reference to req_iter so we can drain it after main() stops.
        stub._req_iter = req_iter
        return _responses()

    stub = MagicMock()
    stub.NodeExec.side_effect = fake_node_exec

    channel = MagicMock()
    monkeypatch.setattr(grpc, "insecure_channel", lambda _srv: channel)
    monkeypatch.setattr(netem_pb2_grpc, "NetemStub", lambda _ch: stub)

    def fake_select(_r, _w, _x, _timeout):
        nonlocal select_calls, in_signal_handler
        select_calls += 1
        if select_calls == 1:
            handler = captured_handlers[signal.SIGWINCH]
            assert callable(handler)
            in_signal_handler = True
            try:
                handler(signal.SIGWINCH, None)
            finally:
                in_signal_handler = False
        return ([], [], [])

    monkeypatch.setattr(select, "select", fake_select)
    monkeypatch.setattr(
        sys, "argv", ["node_terminal_bridge", "localhost:10110", "prj1", "Sat0"]
    )

    node_terminal_bridge.main()

    for msg in stub._req_iter:
        sent_messages.append(msg)

    assert put_called_in_signal_handler is False
    assert ioctl_called_in_signal_handler is False
    resize_msgs = [m for m in sent_messages if m.code == netem_pb2.ExecCltMsg.RESIZE]
    assert len(resize_msgs) == 2
    assert all(m.ttyWidth == 120 and m.ttyHeight == 40 for m in resize_msgs)
