"""Tests for NodeTerminal stream lifecycle (M09)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from satgonetem.services.node_terminal import NodeTerminal


class ScriptedStream:
    """Stream double that yields scripted responses then fails or ends."""

    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.error = error
        self.cancel_calls = 0

    def __iter__(self):
        yield from self.responses
        if self.error is not None:
            raise self.error

    def cancel(self):
        self.cancel_calls += 1


class BlockingStream:
    """Stream double that stays open until cancel() or a short timeout."""

    def __init__(self, timeout=0.5):
        self.timeout = timeout
        self.cancelled = threading.Event()
        self.cancel_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.cancelled.wait(timeout=self.timeout):
            raise StopIteration
        raise StopIteration

    def cancel(self):
        self.cancel_calls += 1
        self.cancelled.set()


class FakeClient:
    def __init__(self, stream):
        self.stream = stream
        self.requests = None

    def NodeExec(self, requests):
        self.requests = requests
        return self.stream


def make_terminal(stream):
    client = FakeClient(stream)
    return NodeTerminal(client, "project", "Sat0"), client


def test_natural_end_of_stream_closes_terminal_and_rejects_send():
    terminal, _ = make_terminal(ScriptedStream())

    with terminal:
        terminal._recv_thread.join(timeout=2)
        assert terminal.read(timeout=0) is None
        with pytest.raises(RuntimeError, match="closed"):
            terminal.send("ls\n")
        with pytest.raises(RuntimeError, match="closed"):
            terminal.resize(80, 24)
        assert terminal.closed is True


def test_receive_error_is_recorded_and_logged(caplog):
    failure = RuntimeError("stream boom")
    terminal, _ = make_terminal(ScriptedStream(error=failure))

    with terminal:
        terminal._recv_thread.join(timeout=2)
        assert "stream boom" in caplog.text
        assert terminal.receive_error is failure
        assert terminal.closed is True
        assert terminal.read(timeout=0) is None

    assert "stream boom" in caplog.text


def test_close_cancels_the_stream_and_stops_the_receiver():
    stream = BlockingStream()
    terminal, _ = make_terminal(stream)

    terminal.__enter__()
    try:
        terminal.close()
    finally:
        terminal.__exit__(None, None, None)

    terminal.close()

    assert stream.cancel_calls == 1
    assert terminal.closed is True
    assert not terminal._recv_thread.is_alive()


def test_close_works_for_a_client_stream_without_cancel():
    terminal, _ = make_terminal(iter([SimpleNamespace(code=0, data=b"hello")]))

    with terminal:
        terminal._recv_thread.join(timeout=2)

    assert terminal.closed is True
