"""Tests for HILManager kernel operations and lifecycle paths.

Covers veth creation, bridge creation/teardown, QoS application and failure
warnings, veth deletion, and the setup/update/teardown lifecycle.

Every external boundary is faked: ``ctypes.CDLL``/``setns``, the namespace
``open`` calls, ``pyroute2.IPRoute``, and the injected ``CommandRunner``.
No root privileges, network namespaces, real interfaces, or subprocesses are
involved. The file is self-contained so it exercises the module's full
behavior on its own; it deliberately does not depend on the sibling
``test_hil_manager.py`` module.
"""

from __future__ import annotations

import builtins
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from satgonetem.launchers import hil_manager
from satgonetem.launchers.hil_manager import (
    CLONE_NEWNET,
    HOST_NS_PATH,
    HILManager,
)


class _FakeNsFile:
    """Minimal file-like object standing in for a namespace file descriptor."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.closed = False

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakeNsFile:
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False


def _install_fake_open(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, _FakeNsFile]
) -> list[str]:
    """Replace ``builtins.open`` for known namespace paths and record lookups."""
    real_open = builtins.open
    opened: list[str] = []

    def fake_open(path, *args, **kwargs):
        key = str(path)
        opened.append(key)
        if key in files:
            return files[key]
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    return opened


def _fake_iproute(
    indexes: dict[str, int], *, fail: Exception | None = None
) -> MagicMock:
    """Build an IPRoute context manager whose ``get`` uses ``indexes``."""
    ipr = MagicMock()
    ipr.__enter__.return_value = ipr
    ipr.__exit__.return_value = False

    if fail is not None:
        ipr.link.side_effect = fail
        return ipr

    def link(action, **kwargs):
        if action != "get":
            return None
        ifname = kwargs.get("ifname")
        index = indexes.get(ifname)
        return [{"index": index}] if index is not None else []

    ipr.link.side_effect = link
    return ipr


def _node(name: str, node_id: int, container_pid: int = 0) -> SimpleNamespace:
    """Build a minimal node exposing only what HILManager reads."""
    return SimpleNamespace(name=name, id=node_id, container_pid=container_pid)


def _link(
    src_name: str,
    tgt_name: str,
    *,
    delay: int = 10,
    src_pid: int = 0,
    src_id: int = 1,
    tgt_id: int = 0,
) -> SimpleNamespace:
    """Build a minimal GroundStationLink-like object."""
    return SimpleNamespace(
        source=_node(src_name, src_id, src_pid),
        target=_node(tgt_name, tgt_id),
        type="GroundStationLink",
        delay=delay,
        peer1_capacity=0,
        peer2_capacity=0,
    )


# _create_veth


def test_create_veth_creates_pair_and_restores_namespace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    libc = MagicMock()
    libc.setns.return_value = 0
    cdll = MagicMock(return_value=libc)
    monkeypatch.setattr(hil_manager, "ctypes", SimpleNamespace(CDLL=cdll))

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    opened = _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )

    sat_ipr = _fake_iproute({"eth3": 101})
    host_ipr = _fake_iproute({"hil3": 202})
    iproute_factory = MagicMock(side_effect=[sat_ipr, host_ipr])
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)
    caplog.set_level(logging.DEBUG)

    assert manager._create_veth("Sat1", 4242, 3) is True

    cdll.assert_called_once_with("libc.so.6", use_errno=True)
    assert libc.setns.call_args_list == [
        call(22, CLONE_NEWNET),
        call(11, CLONE_NEWNET),
    ]
    sat_ipr.link.assert_any_call(
        "add",
        ifname="eth3",
        kind="veth",
        peer={"ifname": "hil3", "net_ns_fd": HOST_NS_PATH},
    )
    sat_ipr.link.assert_any_call("set", index=101, state="up")
    host_ipr.link.assert_any_call("set", index=202, state="up")
    assert iproute_factory.call_count == 2
    assert opened.count("/proc/4242/ns/net") == 1
    assert saved_ns.closed is True
    assert sat_ns.closed is True
    assert "HIL: veth created: eth3 (SAT) <-> hil3 (host)" in caplog.text


def test_create_veth_returns_false_when_setns_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    libc = MagicMock()
    libc.setns.return_value = -1
    monkeypatch.setattr(
        hil_manager, "ctypes", SimpleNamespace(CDLL=MagicMock(return_value=libc))
    )

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )
    iproute_factory = MagicMock()
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)
    caplog.set_level(logging.ERROR)

    assert manager._create_veth("Sat1", 4242, 3) is False

    iproute_factory.assert_not_called()
    assert "failed to enter namespace of Sat1 (pid 4242)" in caplog.text
    assert libc.setns.call_args_list == [call(22, CLONE_NEWNET)]
    assert saved_ns.closed is True
    assert sat_ns.closed is True


def test_create_veth_raises_and_rolls_back_when_namespace_restore_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    libc = MagicMock()
    libc.setns.side_effect = [0, -1, -1]
    monkeypatch.setattr(
        hil_manager, "ctypes", SimpleNamespace(CDLL=MagicMock(return_value=libc))
    )

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )
    sat_ipr = _fake_iproute({"eth3": 101})
    host_ipr = _fake_iproute({"hil3": 202})
    iproute_factory = MagicMock(side_effect=[sat_ipr, host_ipr])
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)
    caplog.set_level(logging.ERROR)

    with pytest.raises(RuntimeError, match="failed to restore host network namespace"):
        manager._create_veth("Sat1", 4242, 3)

    assert libc.setns.call_args_list == [
        call(22, CLONE_NEWNET),
        call(11, CLONE_NEWNET),
        call(11, CLONE_NEWNET),
    ]
    # Host-side setup must not run after a failed restore.
    iproute_factory.assert_called_once()
    # The created veth pair must be deleted so the failure leaves no residue.
    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/4242/ns/net", "ip", "link", "delete", "eth3"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert "failed to restore host network namespace" in caplog.text
    assert saved_ns.closed is True
    assert sat_ns.closed is True


def test_create_veth_returns_false_and_rolls_back_when_sat_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(
        hil_manager, "ctypes", SimpleNamespace(CDLL=MagicMock(return_value=libc))
    )

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )

    sat_ipr = MagicMock()
    sat_ipr.__enter__.return_value = sat_ipr
    sat_ipr.__exit__.return_value = False

    def link(action, **kwargs):
        if action == "add":
            return None
        raise RuntimeError("link lookup failed")

    sat_ipr.link.side_effect = link
    iproute_factory = MagicMock(side_effect=[sat_ipr])
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)

    assert manager._create_veth("Sat1", 4242, 3) is False

    # The pair was added before the failure, so it must be removed again.
    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/4242/ns/net", "ip", "link", "delete", "eth3"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert libc.setns.call_args_list == [
        call(22, CLONE_NEWNET),
        call(11, CLONE_NEWNET),
    ]
    assert saved_ns.closed is True
    assert sat_ns.closed is True


def test_create_veth_raises_when_restore_retry_fails_after_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore failure is fatal even when an earlier setup step failed first.

    The explicit restore check is only reached on the normal path. When the
    satellite-side setup raises, the retry in ``finally`` is the only restore
    attempt; a failure there must not leave the thread in the container
    namespace while returning False.
    """
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    libc = MagicMock()
    # Enter the satellite namespace, then fail the only (retry) restore.
    libc.setns.side_effect = [0, -1]
    monkeypatch.setattr(
        hil_manager, "ctypes", SimpleNamespace(CDLL=MagicMock(return_value=libc))
    )

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )

    sat_ipr = MagicMock()
    sat_ipr.__enter__.return_value = sat_ipr
    sat_ipr.__exit__.return_value = False

    def link(action, **kwargs):
        if action == "add":
            return None
        raise RuntimeError("link lookup failed")

    sat_ipr.link.side_effect = link
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(side_effect=[sat_ipr]))

    with pytest.raises(RuntimeError, match="failed to restore host network namespace"):
        manager._create_veth("Sat1", 4242, 3)

    # The veth pair is rolled back before the fatal error propagates.
    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/4242/ns/net", "ip", "link", "delete", "eth3"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert libc.setns.call_args_list == [
        call(22, CLONE_NEWNET),
        call(11, CLONE_NEWNET),
    ]
    assert saved_ns.closed is True
    assert sat_ns.closed is True


def test_create_veth_returns_false_and_rolls_back_when_host_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(
        hil_manager, "ctypes", SimpleNamespace(CDLL=MagicMock(return_value=libc))
    )

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )
    sat_ipr = _fake_iproute({"eth3": 101})
    host_ipr = _fake_iproute({}, fail=RuntimeError("host setup failed"))
    iproute_factory = MagicMock(side_effect=[sat_ipr, host_ipr])
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)

    assert manager._create_veth("Sat1", 4242, 3) is False

    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/4242/ns/net", "ip", "link", "delete", "eth3"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert iproute_factory.call_count == 2
    assert saved_ns.closed is True
    assert sat_ns.closed is True


def test_create_veth_returns_false_and_restores_namespace_on_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(
        hil_manager, "ctypes", SimpleNamespace(CDLL=MagicMock(return_value=libc))
    )

    saved_ns = _FakeNsFile(11)
    sat_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/self/ns/net": saved_ns, "/proc/4242/ns/net": sat_ns},
    )
    sat_ipr = _fake_iproute({}, fail=RuntimeError("iproute exploded"))
    iproute_factory = MagicMock(return_value=sat_ipr)
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)
    caplog.set_level(logging.ERROR)

    assert manager._create_veth("Sat1", 4242, 3) is False

    iproute_factory.assert_called_once()
    assert "failed to create veth for Sat1<->Gnd3: iproute exploded" in caplog.text
    assert libc.setns.call_args_list == [
        call(22, CLONE_NEWNET),
        call(11, CLONE_NEWNET),
    ]
    assert saved_ns.closed is True
    assert sat_ns.closed is True


# _create_bridge


def test_create_bridge_returns_false_and_leaves_state_untracked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    ipr = _fake_iproute({}, fail=RuntimeError("bridge add failed"))
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(return_value=ipr))
    caplog.set_level(logging.ERROR)

    assert manager._create_bridge("Gnd0", 0) is False

    assert manager._hil_bridges == {}
    assert manager._hil_veths == {}
    assert "HIL: failed to create bridge for Gnd0: bridge add failed" in caplog.text


def _partial_bridge_body_iproute() -> MagicMock:
    """IPRoute that creates the bridge but fails to enslave the hardware iface."""
    indexes = {"brhil0": 30, "hil0": 31, "eth1": 32}

    def link(action, **kwargs):
        if action == "get":
            index = indexes.get(kwargs.get("ifname"))
            return [{"index": index}] if index is not None else []
        if action == "set" and kwargs.get("index") == 32:
            raise RuntimeError("attach failed")
        return None

    ipr = MagicMock()
    ipr.__enter__.return_value = ipr
    ipr.__exit__.return_value = False
    ipr.link.side_effect = link
    return ipr


def test_create_bridge_rolls_back_partial_bridge_when_attach_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    body_ipr = _partial_bridge_body_iproute()
    cleanup_ipr = _fake_iproute({"brhil0": 30})
    monkeypatch.setattr(
        hil_manager, "IPRoute", MagicMock(side_effect=[body_ipr, cleanup_ipr])
    )
    caplog.set_level(logging.ERROR)

    assert manager._create_bridge("Gnd0", 0) is False

    # The bridge created before the failure must be deleted again.
    cleanup_ipr.link.assert_any_call("get", ifname="brhil0")
    cleanup_ipr.link.assert_any_call("del", index=30)
    assert manager._hil_bridges == {}
    assert manager._hil_veths == {}
    assert "HIL: failed to create bridge for Gnd0: attach failed" in caplog.text


def test_create_bridge_keeps_partial_bridge_tracked_when_rollback_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    body_ipr = _partial_bridge_body_iproute()
    cleanup_ipr = _fake_iproute({}, fail=RuntimeError("delete failed"))
    monkeypatch.setattr(
        hil_manager, "IPRoute", MagicMock(side_effect=[body_ipr, cleanup_ipr])
    )
    caplog.set_level(logging.ERROR)

    assert manager._create_bridge("Gnd0", 0) is False

    # The partial bridge stays tracked so a later teardown can retry.
    assert manager._hil_bridges == {"Gnd0": "brhil0"}
    assert "HIL: failed to teardown bridge brhil0: delete failed" in caplog.text


# _apply_qos


def test_apply_qos_warns_when_tc_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = MagicMock()
    manager = HILManager({"Gnd0": "eth1"}, txqueuelen=0, command_runner=runner)
    tc = MagicMock(side_effect=RuntimeError("qdisc add failed"))
    monkeypatch.setattr(hil_manager, "_run_tc_batch", tc)
    caplog.set_level(logging.WARNING)

    manager._apply_qos("Sat2", 5555, 4, delay_ms=20, rate_kbps=8000)

    tc.assert_called_once()
    pid_arg, commands = tc.call_args.args
    assert pid_arg == 5555
    assert commands == [
        "qdisc add dev eth4 root handle 1: netem delay 20ms limit 1000",
        "qdisc add dev eth4 parent 1:1 handle 10: tbf rate 8.000mbit"
        " burst 20000b limit 20000b",
    ]
    assert tc.call_args.kwargs == {"runner": runner}
    assert "HIL: qdisc setup failed on eth4 in Sat2: qdisc add failed" in caplog.text


# _teardown_bridge


def test_teardown_bridge_logs_error_and_keeps_tracking_for_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    manager._hil_bridges["Gnd0"] = "brhil0"
    manager._hil_veths["Gnd0"] = "hil0"
    ipr = _fake_iproute({}, fail=RuntimeError("delete failed"))
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(return_value=ipr))
    caplog.set_level(logging.ERROR)

    manager._teardown_bridge("Gnd0")

    # A failed delete must remain tracked so teardown can be retried.
    assert manager._hil_bridges == {"Gnd0": "brhil0"}
    assert manager._hil_veths == {"Gnd0": "hil0"}
    assert "HIL: failed to teardown bridge brhil0: delete failed" in caplog.text


def test_teardown_bridge_retry_clears_tracking_after_delete_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    manager._hil_bridges["Gnd0"] = "brhil0"
    manager._hil_veths["Gnd0"] = "hil0"
    ipr = _fake_iproute({"brhil0": 40})
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(return_value=ipr))

    manager._teardown_bridge("Gnd0")

    assert manager._hil_bridges == {}
    assert manager._hil_veths == {}


# _delete_sat_veth


def test_delete_sat_veth_logs_error_when_command_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="Cannot find device eth2\n"
    )
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    caplog.set_level(logging.ERROR)

    manager._delete_sat_veth("Sat1", 7777, 2)

    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/7777/ns/net", "ip", "link", "delete", "eth2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert (
        "HIL: failed to delete veth eth2 in Sat1: Cannot find device eth2"
        in caplog.text
    )


# setup_link / update_link error paths


def test_setup_link_stops_when_bridge_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", delay=12, src_pid=4321)
    create_veth = MagicMock(return_value=True)
    create_bridge = MagicMock(return_value=False)
    apply_qos = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_create_veth", create_veth)
    monkeypatch.setattr(manager, "_create_bridge", create_bridge)
    monkeypatch.setattr(manager, "_apply_qos", apply_qos)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)

    manager.setup_link(link)

    create_veth.assert_called_once_with("Sat1", 4321, 0)
    create_bridge.assert_called_once_with("Gnd0", 0)
    # The veth pair created before the bridge failure must be rolled back.
    delete_veth.assert_called_once_with("Sat1", 4321, 0)
    apply_qos.assert_not_called()


def test_update_link_logs_error_when_tc_change_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = MagicMock()
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    link = _link("Sat1", "Gnd0", delay=42, src_pid=9999)
    tc = MagicMock(side_effect=RuntimeError("qdisc change failed"))
    monkeypatch.setattr(hil_manager, "_run_tc_batch", tc)
    caplog.set_level(logging.ERROR)

    manager.update_link(link)

    tc.assert_called_once()
    pid_arg, commands = tc.call_args.args
    assert pid_arg == 9999
    assert commands == [
        "qdisc change dev eth0 root handle 1: netem delay 42ms limit 1000",
        "qdisc change dev eth0 parent 1:1 handle 10: tbf rate 100.000mbit"
        " burst 525000b limit 525000b",
    ]
    assert tc.call_args.kwargs == {"runner": runner}
    assert (
        "HIL: failed to update QoS on eth0 (pid 9999): qdisc change failed"
        in caplog.text
    )


# _run_tc_batch / _run_ip_batch


def test_batch_helpers_run_commands_in_target_namespace() -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)

    hil_manager._run_tc_batch(321, ["qdisc show"], runner=runner)
    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/321/ns/net", "tc", "-batch", "-"],
        input="qdisc show\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    runner.reset_mock()
    hil_manager._run_ip_batch(321, ["link show", "addr show"], runner=runner)
    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/321/ns/net", "ip", "-batch", "-"],
        input="link show\naddr show\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.mark.parametrize(
    ("helper", "tool"),
    [
        (hil_manager._run_tc_batch, "tc"),
        (hil_manager._run_ip_batch, "ip"),
    ],
)
def test_batch_helpers_raise_stderr_on_failure(helper, tool: str) -> None:
    runner = MagicMock()
    runner.run.return_value = SimpleNamespace(returncode=2, stderr=" bad\n")

    with pytest.raises(RuntimeError, match="bad"):
        helper(7, ["show"], runner=runner)

    assert runner.run.call_args.args[0] == [
        "nsenter",
        "--net=/proc/7/ns/net",
        tool,
        "-batch",
        "-",
    ]


# Node and link predicates


def test_is_hil_node_only_matches_mapped_names() -> None:
    manager = HILManager({"Gnd0": "eth1"})

    assert manager.is_hil_node("Gnd0") is True
    assert manager.is_hil_node("Gnd1") is False


def test_is_hil_link_requires_ground_station_type() -> None:
    manager = HILManager({"Gnd0": "eth1"})
    isl = _link("Sat1", "Sat2")
    isl.type = "InterSatelliteLink"

    assert manager.is_hil_link(isl) is False
    assert manager.is_hil_link(_link("Sat1", "Gnd0")) is True
    assert manager.is_hil_link(_link("Gnd0", "Sat1")) is True
    assert manager.is_hil_link(_link("Sat1", "Gnd1")) is False


def test_endpoint_helpers_handle_gnd_on_either_side() -> None:
    manager = HILManager({"Gnd0": "eth1"})
    gnd_source = _link("Gnd0", "Sat1", src_id=0, tgt_id=1)
    gnd_target = _link("Sat1", "Gnd0", src_id=1, tgt_id=0)

    assert manager._get_hil_gnd(gnd_source) is gnd_source.source
    assert manager._get_sat(gnd_source) is gnd_source.target
    assert manager._get_hil_gnd(gnd_target) is gnd_target.target
    assert manager._get_sat(gnd_target) is gnd_target.source


# Bridge creation


def test_create_bridge_attaches_veth_and_hardware(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    ipr = _fake_iproute({"brhil0": 30, "hil0": 31, "eth1": 32})
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(return_value=ipr))
    caplog.set_level(logging.INFO)

    assert manager._create_bridge("Gnd0", 0) is True

    ipr.link.assert_any_call("add", ifname="brhil0", kind="bridge")
    ipr.link.assert_any_call("set", index=30, state="up")
    ipr.link.assert_any_call("set", index=31, master=30)
    ipr.link.assert_any_call("set", index=32, master=30)
    ipr.link.assert_any_call("set", index=32, state="up")
    assert manager._hil_bridges == {"Gnd0": "brhil0"}
    assert manager._hil_veths == {"Gnd0": "hil0"}
    assert "HIL: bridge brhil0 up for Gnd0 (veth=hil0 hw=eth1)" in caplog.text


# txqueuelen handling in _apply_qos


def test_apply_qos_sets_txqueuelen_then_warns_when_it_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = MagicMock()
    manager = HILManager({"Gnd0": "eth1"}, txqueuelen=8000, command_runner=runner)
    ip = MagicMock(side_effect=[None, RuntimeError("txq failed")])
    tc = MagicMock()
    monkeypatch.setattr(hil_manager, "_run_ip_batch", ip)
    monkeypatch.setattr(hil_manager, "_run_tc_batch", tc)
    caplog.set_level(logging.WARNING)

    manager._apply_qos("Sat2", 5555, 4, delay_ms=20, rate_kbps=8000)

    ip.assert_called_once_with(
        5555, ["link set dev eth4 txqueuelen 8000"], runner=runner
    )
    tc.assert_called_once()

    manager._apply_qos("Sat2", 5555, 4, delay_ms=20, rate_kbps=8000)

    assert tc.call_count == 2
    assert "HIL: txqueuelen setup failed on eth4 in Sat2: txq failed" in caplog.text


# Bridge teardown


def test_teardown_bridge_is_noop_without_tracked_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    iproute_factory = MagicMock()
    monkeypatch.setattr(hil_manager, "IPRoute", iproute_factory)

    manager._teardown_bridge("Gnd0")

    iproute_factory.assert_not_called()


def test_teardown_bridge_deletes_bridge_and_clears_tracking(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    manager._hil_bridges["Gnd0"] = "brhil0"
    manager._hil_veths["Gnd0"] = "hil0"
    ipr = _fake_iproute({"brhil0": 40})
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(return_value=ipr))
    caplog.set_level(logging.INFO)

    manager._teardown_bridge("Gnd0")

    ipr.link.assert_any_call("get", ifname="brhil0")
    ipr.link.assert_any_call("del", index=40)
    assert manager._hil_bridges == {}
    assert manager._hil_veths == {}
    assert "HIL: bridge brhil0 torn down for Gnd0" in caplog.text


# veth deletion success


def test_delete_sat_veth_logs_debug_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)
    caplog.set_level(logging.DEBUG)

    manager._delete_sat_veth("Sat1", 7777, 2)

    runner.run.assert_called_once_with(
        ["nsenter", "--net=/proc/7777/ns/net", "ip", "link", "delete", "eth2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert "HIL: deleted veth eth2 in Sat1" in caplog.text


# setup_link lifecycle


def _tracked_container(pid: int, *, running: bool = True) -> SimpleNamespace:
    """Container stand-in exposing the docker ``attrs`` state dict."""
    container = SimpleNamespace(
        attrs={"State": {"Running": running, "Pid": pid}},
    )
    container.reload = MagicMock()
    return container


def test_setup_link_refreshes_stale_pid_from_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    container = _tracked_container(1111)
    container.reload.side_effect = lambda: setattr(
        container, "attrs", {"State": {"Running": True, "Pid": 2222}}
    )
    link.source.container = container
    create_veth = MagicMock(return_value=False)
    monkeypatch.setattr(manager, "_create_veth", create_veth)

    manager.setup_link(link)

    container.reload.assert_called_once_with()
    create_veth.assert_called_once_with("Sat1", 2222, 0)
    assert link.source.container_pid == 2222


def test_setup_link_fails_when_container_is_not_running(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    link.source.container = _tracked_container(1111, running=False)
    create_veth = MagicMock()
    monkeypatch.setattr(manager, "_create_veth", create_veth)
    caplog.set_level(logging.ERROR)

    manager.setup_link(link)

    create_veth.assert_not_called()
    assert "HIL: container for Sat1 is not running" in caplog.text


def test_setup_link_fails_clearly_when_reload_fails_without_cached_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=0)
    container = _tracked_container(1111)
    container.reload.side_effect = RuntimeError("container gone")
    link.source.container = container
    create_veth = MagicMock()
    monkeypatch.setattr(manager, "_create_veth", create_veth)
    caplog.set_level(logging.ERROR)

    manager.setup_link(link)

    create_veth.assert_not_called()
    assert (
        "HIL: could not refresh container state for Sat1: container gone; "
        "no cached container_pid available" in caplog.text
    )
    assert "HIL: no container_pid on Sat1, cannot set up link to Gnd0" in caplog.text


def test_setup_link_falls_back_to_cached_pid_when_reload_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    container = _tracked_container(9999)
    container.reload.side_effect = RuntimeError("container gone")
    link.source.container = container
    create_veth = MagicMock(return_value=False)
    monkeypatch.setattr(manager, "_create_veth", create_veth)
    caplog.set_level(logging.WARNING)

    manager.setup_link(link)

    create_veth.assert_called_once_with("Sat1", 1111, 0)
    assert (
        "HIL: could not refresh container state for Sat1: container gone; "
        "falling back to cached container_pid 1111" in caplog.text
    )


def test_teardown_link_uses_refreshed_pid_for_veth_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    link.source.container = _tracked_container(3333)
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)

    manager.teardown_link(link)

    delete_veth.assert_called_once_with("Sat1", 3333, 0)
    assert link.source.container_pid == 3333


def test_update_link_uses_refreshed_pid_for_tc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    link.source.container = _tracked_container(4444)
    tc = MagicMock()
    monkeypatch.setattr(hil_manager, "_run_tc_batch", tc)

    manager.update_link(link)

    assert tc.call_args.args[0] == 4444
    assert link.source.container_pid == 4444


def test_setup_link_logs_error_and_returns_without_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=0)
    create_veth = MagicMock()
    monkeypatch.setattr(manager, "_create_veth", create_veth)
    caplog.set_level(logging.ERROR)

    manager.setup_link(link)

    create_veth.assert_not_called()
    assert "HIL: no container_pid on Sat1, cannot set up link to Gnd0" in caplog.text


def test_setup_link_returns_when_veth_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=4321)
    monkeypatch.setattr(manager, "_create_veth", MagicMock(return_value=False))
    create_bridge = MagicMock()
    monkeypatch.setattr(manager, "_create_bridge", create_bridge)

    manager.setup_link(link)

    create_bridge.assert_not_called()


def test_setup_link_applies_qos_with_link_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"}, gnd_capacity_kbps=50_000)
    link = _link("Sat1", "Gnd0", delay=12, src_pid=4321)
    link.peer1_capacity = 5000
    monkeypatch.setattr(manager, "_create_veth", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_create_bridge", MagicMock(return_value=True))
    apply_qos = MagicMock()
    monkeypatch.setattr(manager, "_apply_qos", apply_qos)

    manager.setup_link(link)

    apply_qos.assert_called_once_with("Sat1", 4321, 0, 12, 5000)


# teardown_link lifecycle


def test_teardown_link_tears_down_bridge_then_veth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=5678)
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)

    manager.teardown_link(link)

    teardown_bridge.assert_called_once_with("Gnd0")
    delete_veth.assert_called_once_with("Sat1", 5678, 0)


def test_teardown_link_skips_veth_without_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=0)
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)
    caplog.set_level(logging.ERROR)

    manager.teardown_link(link)

    teardown_bridge.assert_called_once_with("Gnd0")
    delete_veth.assert_not_called()
    assert "HIL: no container_pid on Sat1, cannot delete veth for Gnd0" in caplog.text


def test_teardown_link_falls_back_to_cached_pid_when_reload_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    container = _tracked_container(9999)
    container.reload.side_effect = RuntimeError("container gone")
    link.source.container = container
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)
    caplog.set_level(logging.WARNING)

    manager.teardown_link(link)

    teardown_bridge.assert_called_once_with("Gnd0")
    delete_veth.assert_called_once_with("Sat1", 1111, 0)
    assert (
        "HIL: could not refresh container state for Sat1: container gone; "
        "falling back to cached container_pid 1111" in caplog.text
    )


def test_teardown_link_fails_clearly_when_reload_fails_without_cached_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=0)
    container = _tracked_container(1111)
    container.reload.side_effect = RuntimeError("container gone")
    link.source.container = container
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)
    caplog.set_level(logging.ERROR)

    manager.teardown_link(link)

    teardown_bridge.assert_called_once_with("Gnd0")
    delete_veth.assert_not_called()
    assert (
        "HIL: could not refresh container state for Sat1: container gone; "
        "no cached container_pid available" in caplog.text
    )
    assert "HIL: no container_pid on Sat1, cannot delete veth for Gnd0" in caplog.text


def test_teardown_link_skips_veth_when_container_is_stopped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=1111)
    link.source.container = _tracked_container(1111, running=False)
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)
    caplog.set_level(logging.ERROR)

    manager.teardown_link(link)

    teardown_bridge.assert_called_once_with("Gnd0")
    delete_veth.assert_not_called()
    assert "HIL: container for Sat1 is not running" in caplog.text


# update_link success / missing pid


def test_update_link_changes_qdiscs_in_sat_namespace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runner = MagicMock()
    manager = HILManager(
        {"Gnd0": "eth1"}, gnd_capacity_kbps=50_000, command_runner=runner
    )
    link = _link("Sat1", "Gnd0", delay=25, src_pid=9999)
    link.peer1_capacity = 20_000
    tc = MagicMock()
    monkeypatch.setattr(hil_manager, "_run_tc_batch", tc)
    caplog.set_level(logging.DEBUG)

    manager.update_link(link)

    tc.assert_called_once()
    pid_arg, commands = tc.call_args.args
    assert pid_arg == 9999
    assert commands == [
        "qdisc change dev eth0 root handle 1: netem delay 25ms limit 1000",
        "qdisc change dev eth0 parent 1:1 handle 10: tbf rate 20.000mbit"
        " burst 62500b limit 62500b",
    ]
    assert tc.call_args.kwargs == {"runner": runner}
    assert "HIL: updated eth0 in Sat1: delay=25ms rate=20000kbps" in caplog.text


def test_update_link_warns_without_pid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=0)
    tc = MagicMock()
    monkeypatch.setattr(hil_manager, "_run_tc_batch", tc)
    caplog.set_level(logging.WARNING)

    manager.update_link(link)

    tc.assert_not_called()
    assert "HIL: no container_pid on Sat1, cannot update QoS on eth0" in caplog.text


# success/failure return contracts


def test_setup_link_returns_false_when_qos_fails_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=4321)
    monkeypatch.setattr(manager, "_create_veth", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_create_bridge", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_apply_qos", MagicMock(return_value=False))
    teardown_bridge = MagicMock()
    delete_veth = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown_bridge)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)

    assert manager.setup_link(link) is False

    teardown_bridge.assert_called_once_with("Gnd0")
    delete_veth.assert_called_once_with("Sat1", 4321, 0)


@pytest.mark.parametrize(
    ("pid", "veth_ok", "bridge_ok"),
    [(0, True, True), (4321, False, True), (4321, True, False)],
)
def test_setup_link_returns_false_for_missing_pid_or_backend_failure(
    monkeypatch: pytest.MonkeyPatch, pid: int, veth_ok: bool, bridge_ok: bool
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=pid)
    monkeypatch.setattr(manager, "_create_veth", MagicMock(return_value=veth_ok))
    monkeypatch.setattr(manager, "_create_bridge", MagicMock(return_value=bridge_ok))
    monkeypatch.setattr(manager, "_apply_qos", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_delete_sat_veth", MagicMock(return_value=True))

    assert manager.setup_link(link) is False


def test_setup_link_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=4321)
    monkeypatch.setattr(manager, "_create_veth", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_create_bridge", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_apply_qos", MagicMock(return_value=True))

    assert manager.setup_link(link) is True


def test_teardown_link_returns_false_and_keeps_veth_when_bridge_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=5678)
    monkeypatch.setattr(manager, "_teardown_bridge", MagicMock(return_value=False))
    delete_veth = MagicMock(return_value=True)
    monkeypatch.setattr(manager, "_delete_sat_veth", delete_veth)

    assert manager.teardown_link(link) is False
    delete_veth.assert_not_called()


def test_teardown_link_returns_false_when_veth_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=5678)
    monkeypatch.setattr(manager, "_teardown_bridge", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_delete_sat_veth", MagicMock(return_value=False))

    assert manager.teardown_link(link) is False


def test_teardown_link_returns_true_when_everything_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=5678)
    monkeypatch.setattr(manager, "_teardown_bridge", MagicMock(return_value=True))
    monkeypatch.setattr(manager, "_delete_sat_veth", MagicMock(return_value=True))

    assert manager.teardown_link(link) is True


def test_update_link_returns_false_without_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=0)

    assert manager.update_link(link) is False


def test_update_link_returns_false_when_tc_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=9999)
    monkeypatch.setattr(
        hil_manager, "_run_tc_batch", MagicMock(side_effect=RuntimeError("boom"))
    )

    assert manager.update_link(link) is False


def test_update_link_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=9999)
    monkeypatch.setattr(hil_manager, "_run_tc_batch", MagicMock(return_value=None))

    assert manager.update_link(link) is True


def test_apply_qos_returns_false_when_tc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HILManager({"Gnd0": "eth1"}, txqueuelen=0)
    monkeypatch.setattr(
        hil_manager, "_run_tc_batch", MagicMock(side_effect=RuntimeError("boom"))
    )

    assert manager._apply_qos("Sat1", 4321, 0, delay_ms=10, rate_kbps=1000) is False


def test_apply_qos_returns_false_when_txqueuelen_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"}, txqueuelen=8000)
    monkeypatch.setattr(
        hil_manager, "_run_ip_batch", MagicMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(hil_manager, "_run_tc_batch", MagicMock(return_value=None))

    assert manager._apply_qos("Sat1", 4321, 0, delay_ms=10, rate_kbps=1000) is False


def test_teardown_bridge_returns_false_when_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    manager._hil_bridges["Gnd0"] = "brhil0"
    ipr = _fake_iproute({}, fail=RuntimeError("delete failed"))
    monkeypatch.setattr(hil_manager, "IPRoute", MagicMock(return_value=ipr))

    assert manager._teardown_bridge("Gnd0") is False


def test_delete_sat_veth_returns_false_when_command_fails() -> None:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="boom"
    )
    manager = HILManager({"Gnd0": "eth1"}, command_runner=runner)

    assert manager._delete_sat_veth("Sat1", 7777, 2) is False


def test_wire_links_raises_when_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    link = _link("Sat1", "Gnd0", src_pid=111)
    monkeypatch.setattr(manager, "setup_link", MagicMock(return_value=False))

    with pytest.raises(RuntimeError, match="failed to set up link Sat1<->Gnd0"):
        manager.wire_links([link])


# wire_links / teardown_all


def test_wire_links_sets_up_every_link(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    links = [
        _link("Sat1", "Gnd0", src_pid=111),
        _link("Sat2", "Gnd0", src_pid=222),
    ]
    setup = MagicMock()
    monkeypatch.setattr(manager, "setup_link", setup)

    manager.wire_links(links)

    assert setup.call_args_list == [call(links[0]), call(links[1])]


def test_teardown_all_tears_down_each_tracked_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = HILManager({"Gnd0": "eth1"})
    manager._hil_bridges = {"Gnd0": "brhil0", "Gnd2": "brhil2"}
    teardown = MagicMock()
    monkeypatch.setattr(manager, "_teardown_bridge", teardown)

    manager.teardown_all()

    assert teardown.call_args_list == [call("Gnd0"), call("Gnd2")]
