"""Characterization tests for P4Daemon controller process control.

External boundaries (filesystem checks, ``chmod``, ``Popen``, and the
post-launch grace sleep) are patched so the tests never launch a real process.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import threading
from multiprocessing import shared_memory
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from satgonetem.models.interface import Interface
from satgonetem.routing import p4_daemon
from satgonetem.routing.p4_daemon import P4Daemon, safe_int, safe_ip
from tests.support import make_link, make_satellite

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


class RacyStderr:
    """A stderr stand-in where a second reader consumes the failure output.

    The reader thread blocks until the process is stopped (or the timeout
    expires). A concurrent ``read()`` before that point wins the race and
    discards the pending text, reproducing the lost-output failure mode.
    """

    def __init__(self, message):
        self._message = message
        self._released = threading.Event()
        self._consumed_by_read = False
        self.read_calls = 0

    def release(self):
        self._released.set()

    def readline(self):
        self._released.wait(timeout=2)
        if self._consumed_by_read:
            return ""
        message, self._message = self._message, ""
        return message

    def read(self):
        self.read_calls += 1
        if not self._released.is_set():
            self._consumed_by_read = True
            self._message = ""
            return ""
        message, self._message = self._message, ""
        return message


class RacyProcess(FakeProcess):
    """Fake controller process whose termination releases the stderr reader."""

    def __init__(self, stderr):
        super().__init__(returncode=3)
        self.stderr = stderr

    def terminate(self):
        super().terminate()
        self.stderr.release()


def _make_export_link(source, target):
    """Build a link that can be exported without touching a real node."""
    link = make_link(source, target)
    link.peer_interfaces = [
        Interface(name=f"{source.name}.{target.id}"),
        Interface(name=f"{target.name}.{source.id}"),
    ]
    for iface in link.peer_interfaces:
        iface.set_mac_address("")
    return link


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
    return P4Daemon(
        topology,
        str(tmp_path / "controller"),
        readiness_probe=lambda: True,
    )


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


def test_start_returns_false_when_controller_script_is_missing(daemon, monkeypatch):
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
    _patch_sleep(monkeypatch)
    script_path = str(tmp_path / "controller" / "run_controller.sh")

    assert daemon.start_p4_controller() is True

    assert chmod_calls == [(script_path, 0o755)]
    popen.assert_called_once()
    assert popen.call_args.args[0] == [script_path]
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


def test_start_preserves_early_exit_stderr_without_concurrent_read(
    daemon, monkeypatch, caplog
):
    _patch_os(monkeypatch, exists=True, executable=True)
    stderr = RacyStderr("controller boot failure")
    process = RacyProcess(stderr)
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)

    with caplog.at_level(logging.ERROR):
        assert daemon.start_p4_controller() is False

    assert stderr.read_calls == 0
    assert "controller boot failure" in caplog.text
    assert daemon.p4_controller_process is None


def test_controller_stderr_is_logged_once_at_error_on_failure(
    daemon, monkeypatch, caplog
):
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=3, stderr="controller boot failure\n")
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)

    with caplog.at_level(logging.ERROR):
        assert daemon.start_p4_controller() is False

    matches = [
        record
        for record in caplog.records
        if "controller boot failure" in record.getMessage()
    ]
    assert len(matches) == 1
    assert matches[0].levelno == logging.ERROR


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

    graph_cls.assert_called_once_with(name="satgonetem_graph", max_edges=2, create=True)
    graph.close.assert_called_once_with()
    graph.unlink.assert_called_once_with()
    assert daemon.graph_shared_memory is None


def test_init_releases_everything_when_readiness_probe_raises(
    topology, tmp_path, monkeypatch, caplog
):
    name = f"satgonetem-test-p4-{os.getpid()}-{uuid4().hex}"
    probe = MagicMock(side_effect=RuntimeError("probe exploded"))
    daemon = P4Daemon(
        topology,
        str(tmp_path / "controller"),
        shared_memory_name=name,
        readiness_probe=probe,
    )
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=None)
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)

    with caplog.at_level(logging.ERROR):
        assert daemon.init() is False

    assert "probe exploded" in caplog.text
    assert process.terminate_calls == 1
    assert daemon.p4_controller_process is None
    assert daemon.graph_shared_memory is None
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)


def test_init_returns_true_when_controller_starts(daemon, monkeypatch):
    daemon.topology.links = {0: object()}
    graph_cls = MagicMock(return_value=object())
    monkeypatch.setattr(p4_daemon, "GraphSharedMemory", graph_cls)
    monkeypatch.setattr(daemon, "start_p4_controller", lambda timeout=10: True)

    assert daemon.init() is True
    graph_cls.assert_called_once_with(name="satgonetem_graph", max_edges=1, create=True)


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


def test_remove_with_node_does_not_tear_down_shared_controller(daemon, caplog):
    graph = MagicMock()
    process = FakeProcess()
    daemon.graph_shared_memory = graph
    daemon.p4_controller_process = process

    with caplog.at_level(logging.WARNING):
        daemon.remove(make_satellite("Sat0"))

    assert "does not support per-node route removal" in caplog.text
    assert process.terminate_calls == 0
    assert graph.close.call_count == 0
    assert graph.unlink.call_count == 0
    assert daemon.graph_shared_memory is graph
    assert daemon.p4_controller_process is process


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
        readiness_probe=lambda: True,
    )
    _patch_os(monkeypatch, exists=True, executable=True)
    _patch_sleep(monkeypatch)

    assert daemon.start_p4_controller() is True
    assert runner.calls
    assert runner.calls[0][0] == [str(tmp_path / "controller" / "run_controller.sh")]


def test_start_checks_readiness_probe_before_returning_true(daemon, monkeypatch):
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=None)
    _patch_popen(monkeypatch, process=process)
    sleep = _patch_sleep(monkeypatch)
    probe = MagicMock(side_effect=[False, False, True])
    monkeypatch.setattr(daemon, "_readiness_probe", probe, raising=False)

    assert daemon.start_p4_controller(timeout=10) is True

    assert probe.call_count == 3
    assert sleep.call_count == 2
    assert daemon.p4_controller_process is process


def test_start_returns_false_and_stops_controller_when_readiness_times_out(
    daemon, monkeypatch, caplog
):
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=None)
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)
    probe = MagicMock(return_value=False)
    monkeypatch.setattr(daemon, "_readiness_probe", probe, raising=False)

    with caplog.at_level(logging.ERROR):
        assert daemon.start_p4_controller(timeout=0) is False

    assert probe.call_count == 1
    assert "did not become ready" in caplog.text
    assert process.terminate_calls == 1
    assert daemon.p4_controller_process is None


def test_start_accepts_injected_readiness_probe(topology, tmp_path, monkeypatch):
    _patch_os(monkeypatch, exists=True, executable=True)
    process = FakeProcess(returncode=None)
    _patch_popen(monkeypatch, process=process)
    _patch_sleep(monkeypatch)
    probe = MagicMock(return_value=True)
    daemon = P4Daemon(
        topology,
        str(tmp_path / "controller"),
        readiness_probe=probe,
    )

    assert daemon.start_p4_controller() is True
    probe.assert_called_once_with()


def test_default_readiness_probe_checks_controller_port(
    topology, tmp_path, monkeypatch
):
    daemon = P4Daemon(
        topology,
        str(tmp_path / "controller"),
        controller_port=9559,
    )
    connection = MagicMock()
    create_connection = MagicMock(return_value=connection)
    monkeypatch.setattr(p4_daemon.socket, "create_connection", create_connection)

    assert daemon._probe_controller_port() is True

    create_connection.assert_called_once_with(("127.0.0.1", 9559), timeout=0.5)
    connection.__enter__.assert_called_once_with()


def test_default_readiness_probe_returns_false_when_port_is_unreachable(
    topology, tmp_path, monkeypatch
):
    daemon = P4Daemon(topology, str(tmp_path / "controller"))
    create_connection = MagicMock(side_effect=OSError("connection refused"))
    monkeypatch.setattr(p4_daemon.socket, "create_connection", create_connection)

    assert daemon._probe_controller_port() is False


def test_update_before_init_is_a_documented_noop(daemon, caplog):
    assert daemon.graph_shared_memory is None

    with caplog.at_level(logging.WARNING):
        assert daemon.update([]) is None

    assert "not initialized" in caplog.text
    assert daemon.graph_shared_memory is None


def test_update_exports_only_active_links_not_pending_removal(daemon, monkeypatch):
    graph = MagicMock()
    daemon.graph_shared_memory = graph
    active = SimpleNamespace(is_active=True, to_remove=False, tag="active")
    inactive = SimpleNamespace(is_active=False, to_remove=False, tag="inactive")
    pending_removal = SimpleNamespace(is_active=True, to_remove=True, tag="pending")
    daemon.topology.links = {0: active, 1: inactive, 2: pending_removal}
    monkeypatch.setattr(p4_daemon, "build_edge_from_link", lambda link: link.tag)

    daemon.update([])

    assert graph.write_edges.call_args.args[0] == ["active"]


def test_update_excludes_links_disabled_in_the_current_graph(daemon, monkeypatch):
    graph = MagicMock()
    daemon.graph_shared_memory = graph
    active = SimpleNamespace(
        is_active=True,
        to_remove=False,
        satcom_object=SimpleNamespace(disabled=False),
        tag="active",
    )
    satcom_disabled = SimpleNamespace(
        is_active=True,
        to_remove=False,
        satcom_object=SimpleNamespace(disabled=True),
        tag="satcom-disabled",
    )
    link_disabled = SimpleNamespace(
        is_active=True,
        to_remove=False,
        disabled=True,
        tag="link-disabled",
    )
    daemon.topology.links = {
        0: active,
        1: satcom_disabled,
        2: link_disabled,
    }
    monkeypatch.setattr(p4_daemon, "build_edge_from_link", lambda link: link.tag)

    daemon.update([])

    assert graph.write_edges.call_args.args[0] == ["active"]


def test_update_grows_shared_memory_when_links_exceed_init_capacity(
    topology, tmp_path, monkeypatch
):
    name = f"satgonetem-test-p4-{os.getpid()}-{uuid4().hex}"
    daemon = P4Daemon(topology, str(tmp_path / "controller"), shared_memory_name=name)
    topology.links = {
        0: _make_export_link(make_satellite("Sat0"), make_satellite("Sat1"))
    }
    monkeypatch.setattr(daemon, "start_p4_controller", lambda timeout=10: True)

    try:
        assert daemon.init() is True
        graph = daemon.graph_shared_memory
        assert graph.max_edges == 1

        topology.links[1] = _make_export_link(
            make_satellite("Sat2"), make_satellite("Sat3")
        )
        daemon.update([])

        graph = daemon.graph_shared_memory
        assert graph.max_edges >= 2
        assert graph.header[0]["num_edges"] == 2
        assert graph.header[0]["max_edges"] == graph.max_edges
        assert {int(edge["src_name"]) for edge in graph.edges[:2]} == {0, 2}
    finally:
        graph = daemon.graph_shared_memory
        if graph is not None:
            graph.close()
            graph.unlink()
