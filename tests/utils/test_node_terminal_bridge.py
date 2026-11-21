"""Characterization tests for the node terminal bridge helpers.

The interactive ``main`` path requires a real TTY, a running GoNetEm gRPC
service, and terminal signal handling. Only the argument-validation path and
the pure helpers are exercised here; no terminal is opened.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import mock_open

import pytest

from satgonetem.utils import node_terminal_bridge

pytestmark = pytest.mark.unit


def test_find_python_returns_the_current_interpreter():
    python = node_terminal_bridge._find_python()

    assert isinstance(python, str)
    assert python
    assert python == sys.executable


def test_log_error_writes_message_to_stderr_and_log_file(monkeypatch, capsys):
    log_file = mock_open()
    monkeypatch.setattr("builtins.open", log_file)

    node_terminal_bridge._log_error("bridge failure")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "bridge failure\r\n"
    log_file.assert_called_once_with(f"/tmp/satgonetem_bridge_{os.getpid()}.log", "a")
    log_file().write.assert_called_once_with("bridge failure\n")


def test_log_error_still_writes_stderr_when_log_file_cannot_be_opened(
    monkeypatch, capsys
):
    def failing_open(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr("builtins.open", failing_open)

    node_terminal_bridge._log_error("visible anyway")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "visible anyway\r\n"


def test_main_prints_usage_to_stderr_and_exits_without_arguments(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["node_terminal_bridge"])

    with pytest.raises(SystemExit) as excinfo:
        node_terminal_bridge.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Usage:" in captured.err
    assert "node_terminal_bridge <server> <project_id> <node_name>" in captured.err
