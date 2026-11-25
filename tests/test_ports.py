"""Tests for the default external-boundary adapters."""

from __future__ import annotations

import builtins
import ctypes
import sys
from unittest.mock import MagicMock, call

import pyroute2
import pytest

from satgonetem.ports import LinuxNetworkNamespace, SubprocessRunner

pytestmark = pytest.mark.unit


def test_subprocess_runner_returns_captured_output():
    result = SubprocessRunner().run(
        [sys.executable, "-c", "print('ok')"],
    )

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""


def test_subprocess_runner_honours_input_and_check():
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        input="hello\n",
        check=True,
    )

    assert result.stdout == "hello\n"


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
) -> None:
    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        key = str(path)
        if key in files:
            return files[key]
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


def _fake_iproute() -> MagicMock:
    ipr = MagicMock()
    ipr.__enter__.return_value = ipr
    ipr.__exit__.return_value = False
    ipr.link_lookup.return_value = [7]
    return ipr


def test_create_dummy_interface_raises_when_host_namespace_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.side_effect = [0, -1]
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    target_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/thread-self/ns/net": saved_ns, "/proc/123/ns/net": target_ns},
    )
    ipr = _fake_iproute()
    monkeypatch.setattr(pyroute2, "IPRoute", MagicMock(return_value=ipr))

    with pytest.raises(OSError, match="host network namespace"):
        LinuxNetworkNamespace().create_dummy_interface(123, "dummy0")

    assert libc.setns.call_args_list == [
        call(22, LinuxNetworkNamespace.CLONE_NEWNET),
        call(11, LinuxNetworkNamespace.CLONE_NEWNET),
    ]
    ipr.link.assert_any_call("add", ifname="dummy0", kind="dummy")
    assert saved_ns.closed is True
    assert target_ns.closed is True


def test_create_veth_raises_when_host_namespace_restore_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.side_effect = [0, 0, -1]
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    source_ns = _FakeNsFile(22)
    destination_ns = _FakeNsFile(33)
    _install_fake_open(
        monkeypatch,
        {
            "/proc/thread-self/ns/net": saved_ns,
            "/proc/10/ns/net": source_ns,
            "/proc/11/ns/net": destination_ns,
        },
    )
    ipr = _fake_iproute()
    monkeypatch.setattr(pyroute2, "IPRoute", MagicMock(return_value=ipr))

    with pytest.raises(OSError, match="host network namespace"):
        LinuxNetworkNamespace().create_veth(10, 11, "eth0", "eth1")

    assert libc.setns.call_args_list == [
        call(22, LinuxNetworkNamespace.CLONE_NEWNET),
        call(33, LinuxNetworkNamespace.CLONE_NEWNET),
        call(11, LinuxNetworkNamespace.CLONE_NEWNET),
    ]
    ipr.link.assert_any_call(
        "add",
        ifname="eth0",
        kind="veth",
        peer={"ifname": "eth1", "net_ns_fd": "/proc/11/ns/net"},
    )
    assert saved_ns.closed is True
    assert source_ns.closed is True
    assert destination_ns.closed is True


def test_create_dummy_interface_does_not_restore_after_failed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.return_value = -1
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    target_ns = _FakeNsFile(22)
    _install_fake_open(
        monkeypatch,
        {"/proc/thread-self/ns/net": saved_ns, "/proc/123/ns/net": target_ns},
    )
    iproute_factory = MagicMock()
    monkeypatch.setattr(pyroute2, "IPRoute", iproute_factory)

    with pytest.raises(OSError, match="setns failed for PID 123"):
        LinuxNetworkNamespace().create_dummy_interface(123, "dummy0")

    iproute_factory.assert_not_called()
    # Entering the target namespace failed, so the thread never left the host
    # namespace and must not issue a second setns call.
    assert libc.setns.call_args_list == [
        call(22, LinuxNetworkNamespace.CLONE_NEWNET),
    ]
    assert saved_ns.closed is True
    assert target_ns.closed is True


def test_create_dummy_interface_opens_thread_self_netns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    target_ns = _FakeNsFile(22)
    opened: list[str] = []

    def fake_open(path, *args, **kwargs):
        key = str(path)
        opened.append(key)
        if key == "/proc/thread-self/ns/net":
            return saved_ns
        if key == "/proc/123/ns/net":
            return target_ns
        raise AssertionError(f"Unexpected path opened: {key}")

    monkeypatch.setattr(builtins, "open", fake_open)
    ipr = _fake_iproute()
    monkeypatch.setattr(pyroute2, "IPRoute", MagicMock(return_value=ipr))

    LinuxNetworkNamespace().create_dummy_interface(123, "dummy0")

    assert "/proc/thread-self/ns/net" in opened
    assert "/proc/self/ns/net" not in opened
    assert saved_ns.closed is True


def test_create_veth_opens_thread_self_netns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    source_ns = _FakeNsFile(22)
    destination_ns = _FakeNsFile(33)
    opened: list[str] = []

    def fake_open(path, *args, **kwargs):
        key = str(path)
        opened.append(key)
        mapping = {
            "/proc/thread-self/ns/net": saved_ns,
            "/proc/10/ns/net": source_ns,
            "/proc/11/ns/net": destination_ns,
        }
        if key in mapping:
            return mapping[key]
        raise AssertionError(f"Unexpected path opened: {key}")

    monkeypatch.setattr(builtins, "open", fake_open)
    ipr = _fake_iproute()
    monkeypatch.setattr(pyroute2, "IPRoute", MagicMock(return_value=ipr))

    LinuxNetworkNamespace().create_veth(10, 11, "eth0", "eth1")

    assert "/proc/thread-self/ns/net" in opened
    assert "/proc/self/ns/net" not in opened
    assert saved_ns.closed is True


def test_create_dummy_interface_falls_back_to_proc_self_when_thread_self_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    target_ns = _FakeNsFile(22)
    opened: list[str] = []

    def fake_open(path, *args, **kwargs):
        key = str(path)
        opened.append(key)
        if key == "/proc/thread-self/ns/net":
            raise FileNotFoundError(key)
        if key == "/proc/self/ns/net":
            return saved_ns
        if key == "/proc/123/ns/net":
            return target_ns
        raise AssertionError(f"Unexpected path opened: {key}")

    monkeypatch.setattr(builtins, "open", fake_open)
    ipr = _fake_iproute()
    monkeypatch.setattr(pyroute2, "IPRoute", MagicMock(return_value=ipr))

    LinuxNetworkNamespace().create_dummy_interface(123, "dummy0")

    assert opened[:2] == ["/proc/thread-self/ns/net", "/proc/self/ns/net"]
    assert saved_ns.closed is True


def test_create_veth_cleans_up_on_destination_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    src_ns = _FakeNsFile(22)
    dst_ns = _FakeNsFile(33)

    def fake_open(path, *args, **kwargs):
        mapping = {
            "/proc/thread-self/ns/net": saved_ns,
            "/proc/10/ns/net": src_ns,
            "/proc/11/ns/net": dst_ns,
        }
        return mapping[str(path)]

    monkeypatch.setattr(builtins, "open", fake_open)

    ipr_src = _fake_iproute()
    ipr_dst = _fake_iproute()
    ipr_dst.link_lookup.return_value = []  # triggers failure in destination ns
    ipr_cleanup = _fake_iproute()
    monkeypatch.setattr(
        pyroute2, "IPRoute", MagicMock(side_effect=[ipr_src, ipr_dst, ipr_cleanup])
    )

    with pytest.raises(RuntimeError, match="eth1"):
        LinuxNetworkNamespace().create_veth(10, 11, "eth0", "eth1")

    ipr_cleanup.link.assert_any_call("del", index=7)


def test_create_dummy_interface_cleans_up_on_set_up_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = MagicMock()
    libc.setns.return_value = 0
    monkeypatch.setattr(ctypes, "CDLL", MagicMock(return_value=libc))

    saved_ns = _FakeNsFile(11)
    target_ns = _FakeNsFile(22)
    monkeypatch.setattr(
        builtins,
        "open",
        lambda path, *a, **k: {
            "/proc/thread-self/ns/net": saved_ns,
            "/proc/123/ns/net": target_ns,
        }[str(path)],
    )

    ipr = _fake_iproute()

    def link_side_effect(action, **kwargs):
        if action == "set":
            raise OSError("set up failed")
        return None

    ipr.link.side_effect = link_side_effect
    monkeypatch.setattr(pyroute2, "IPRoute", MagicMock(return_value=ipr))

    with pytest.raises(OSError, match="set up failed"):
        LinuxNetworkNamespace().create_dummy_interface(123, "dummy0")

    ipr.link.assert_any_call("del", index=7)
