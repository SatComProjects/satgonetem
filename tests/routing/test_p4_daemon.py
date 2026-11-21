"""Characterization tests for P4Daemon controller process control.

External boundaries (filesystem checks, ``chmod``, ``Popen``, and the
post-launch grace sleep) are patched so the tests never launch a real process.
"""

from __future__ import annotations

import io
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from satgonetem.routing import p4_daemon
from satgonetem.routing.p4_daemon import P4Daemon, safe_int, safe_ip

pytestmark = pytest.mark.unit


class FakeProcess:
    """Minimal stand-in for ``subprocess.Popen`` used by the daemon."""

    def __init__(self, *, returncode=None, stderr="", wait_errors=None):
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO(stderr)
        self.pid = 4242
        self.returncode = returncode
        self._wait_errors = list(wait_errors or [])
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._wait_errors:
            raise self._wait_errors.pop(0)
        return self.returncode


def _patch_os(
    monkeypatch,
    *,
    exists=True,
    executable=True,
    chmod_error=None,
):
    """Replace the module's ``os`` view with scriptable filesystem stubs."""
    chmod_calls = []

    def fake_chmod(path, mode):
        chmod_calls.append((path, mode))
        if chmod_error is not None:
            raise chmod_error

    fake_os = SimpleNamespace(
        path=SimpleNamespace(join=os.path.join, exists=lambda path: exists),
        access=lambda path, mode: executable,
        chmod=fake_chmod,
        stat=lambda path: SimpleNamespace(st_mode=0o644),
        X_OK=os.X_OK,
    )
    monkeypatch.setattr(p4_daemon, "os", fake_os)
    return chmod_calls


def _patch_popen(monkeypatch, *, process=None, error=None):
    def fake_popen(*args, **kwargs):
        if error is not None:
            raise error
        return process

    popen = MagicMock(side_effect=fake_popen)
    monkeypatch.setattr(p4_daemon.subprocess, "Popen", popen)
    return popen


def _patch_sleep(monkeypatch):
    sleep = MagicMock()
    monkeypatch.setattr(p4_daemon.time, "sleep", sleep)
    return sleep


@pytest.fixture
def daemon(topology, tmp_path):
    """A daemon whose controller directory is never touched on disk."""
    return P4Daemon(topology, str(tmp_path / "controller"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0),
        (0, 0),
        (5, 5),
        ("12", 12),
    ],
)
def test_safe_int_converts_values_and_defaults_none(value, expected):
    assert safe_int(value) == expected


def test_safe_int_honours_custom_default_for_none():
    assert safe_int(None, default=7) == 7


def test_safe_int_raises_for_non_numeric_text():
    with pytest.raises(ValueError):
        safe_int("Sat0")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "0.0.0.0"),
        ("10.0.0.1", "10.0.0.1"),
        ("", "0.0.0.0"),
    ],
)
def test_safe_ip_replaces_missing_values(value, expected):
    assert safe_ip(value) == expected


def test_start_returns_false_when_controller_script_is_missing(
    daemon, monkeypatch
):
    _patch_os(monkeypatch, exists=False)
    popen = _patch_popen(monkeypatch)
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is False
    popen.assert_not_called()
    assert daemon.p4_controller_process is None


def test_start_sets_executable_bit_when_script_is_not_executable(
    daemon, monkeypatch, tmp_path
):
    chmod_calls = _patch_os(monkeypatch, exists=True, executable=False)
    process = FakeProcess(returncode=None)
    popen = _patch_popen(monkeypatch, process=process)
    sleep = _patch_sleep(monkeypatch)
    script_path = str(tmp_path / "controller" / "run_controller.sh")

    assert daemon.start_p4_controller() is True

    assert chmod_calls == [(script_path, 0o755)]
    popen.assert_called_once()
    assert popen.call_args.args[0] == [script_path]
    sleep.assert_called_once_with(0.5)
    assert daemon.p4_controller_process is process


def test_start_returns_false_when_chmod_fails(daemon, monkeypatch):
    _patch_os(
        monkeypatch,
        exists=True,
        executable=False,
        chmod_error=OSError("operation not permitted"),
    )
    popen = _patch_popen(monkeypatch)
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is False
    popen.assert_not_called()
    assert daemon.p4_controller_process is None


def test_start_returns_false_when_popen_raises(daemon, monkeypatch):
    _patch_os(monkeypatch, exists=True, executable=True)
    _patch_popen(monkeypatch, error=OSError("cannot execute"))
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is False
    assert daemon.p4_controller_process is None


def test_start_detects_immediate_exit_and_stops_process(daemon, monkeypatch):
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=3, stderr="controller boot failure")
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is False

    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0
    assert daemon.p4_controller_process is None


def test_start_reports_running_process(daemon, monkeypatch):
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=None)
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is True
    assert daemon.p4_controller_process is process
    assert daemon.is_p4_controller_running() is True


def test_is_p4_controller_running_false_without_process(daemon):
    assert daemon.is_p4_controller_running() is False


def test_is_p4_controller_running_false_after_process_exit(daemon):
    daemon.p4_controller_process = FakeProcess(returncode=0)

    assert daemon.is_p4_controller_running() is False


def test_stop_terminates_process_and_clears_handle(daemon):
    process = FakeProcess(returncode=None)
    daemon.p4_controller_process = process

    daemon.stop_p4_controller(timeout=2)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 1
    assert daemon.p4_controller_process is None


def test_stop_kills_process_after_wait_timeout(daemon):
    process = FakeProcess(
        returncode=None,
        wait_errors=[subprocess.TimeoutExpired(["run_controller.sh"], 2)],
    )
    daemon.p4_controller_process = process

    daemon.stop_p4_controller(timeout=2)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert daemon.p4_controller_process is None


def test_init_returns_false_when_controller_fails(daemon, monkeypatch):
    daemon.topology.links = {0: object(), 1: object()}
    graph = MagicMock()
    graph_cls = MagicMock(return_value=graph)
    monkeypatch.setattr(p4_daemon, "GraphSharedMemory", graph_cls)
    monkeypatch.setattr(daemon, "start_p4_controller", lambda timeout=10: False)

    assert daemon.init() is False

    graph_cls.assert_called_once_with(
        name="satgonetem_graph", max_edges=2, create=True
    )
    graph.close.assert_called_once_with()
    graph.unlink.assert_called_once_with()
    assert daemon.graph_shared_memory is None


def test_init_returns_true_when_controller_starts(daemon, monkeypatch):
    daemon.topology.links = {0: object()}
    graph_cls = MagicMock(return_value=object())
    monkeypatch.setattr(p4_daemon, "GraphSharedMemory", graph_cls)
    monkeypatch.setattr(daemon, "start_p4_controller", lambda timeout=10: True)

    assert daemon.init() is True
    graph_cls.assert_called_once_with(
        name="satgonetem_graph", max_edges=1, create=True
    )


def test_init_reinitializes_owned_shared_memory(daemon, monkeypatch):
    old_graph = MagicMock()
    new_graph = MagicMock()
    daemon.topology.links = {0: object()}
    daemon.graph_shared_memory = old_graph
    graph_cls = MagicMock(return_value=new_graph)
    monkeypatch.setattr(p4_daemon, "GraphSharedMemory", graph_cls)
    monkeypatch.setattr(daemon, "start_p4_controller", lambda timeout=10: True)

    assert daemon.init() is True

    old_graph.close.assert_called_once_with()
    old_graph.unlink.assert_called_once_with()
    assert daemon.graph_shared_memory is new_graph


def test_init_uses_custom_shared_memory_name(monkeypatch, topology, tmp_path):
    custom = P4Daemon(
        topology,
        str(tmp_path / "controller"),
        shared_memory_name="satgonetem-test-graph",
    )
    custom.topology.links = {0: object()}
    graph_cls = MagicMock(return_value=object())
    monkeypatch.setattr(p4_daemon, "GraphSharedMemory", graph_cls)
    monkeypatch.setattr(custom, "start_p4_controller", lambda timeout=10: True)

    assert custom.init() is True
    graph_cls.assert_called_once_with(
        name="satgonetem-test-graph", max_edges=1, create=True
    )


def test_remove_stops_controller_and_releases_shared_memory(daemon):
    graph = MagicMock()
    process = FakeProcess()
    daemon.graph_shared_memory = graph
    daemon.p4_controller_process = process

    daemon.remove()

    assert process.terminate_calls == 1
    graph.close.assert_called_once_with()
    graph.unlink.assert_called_once_with()
    assert daemon.graph_shared_memory is None
    assert daemon.p4_controller_process is None


def test_start_p4_controller_uses_injected_process_runner(
    topology, tmp_path, monkeypatch
):
    class RecordingRunner:
        def __init__(self, process):
            self.process = process
            self.calls = []

        def popen(self, args, **kwargs):
            self.calls.append((args, kwargs))
            return self.process

    process = FakeProcess()
    runner = RecordingRunner(process)
    daemon = P4Daemon(
        topology,
        str(tmp_path / "controller"),
        process_runner=runner,
    )
    _patch_os(monkeypatch, exists=True, executable=True)
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is True
    assert runner.calls
    assert runner.calls[0][0] == [str(tmp_path / "controller" / "run_controller.sh")]
