"""Root-only veth/setns round trip across two disposable container namespaces."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from satgonetem.ports import LinuxNetworkNamespace
from tests.support.live import LiveEnvironment, unique_project_name

pytestmark = [pytest.mark.integration, pytest.mark.root]

_SOURCE_INTERFACE = "livenet0"
_DESTINATION_INTERFACE = "livenet1"


def _wait_for_running(container, timeout: float = 15.0) -> int:
    """Return the container PID once it is running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container.reload()
        pid = int(container.attrs.get("State", {}).get("Pid") or 0)
        if container.status == "running" and pid > 0:
            return pid
        time.sleep(0.1)
    raise RuntimeError(
        f"container {container.name} did not reach running state with a PID"
    )


def _nsenter_ip_link(pid: int, interface: str) -> str:
    """Run ``ip link show`` in *pid*'s network namespace via host nsenter."""
    result = subprocess.run(
        ["nsenter", f"--net=/proc/{pid}/ns/net", "ip", "link", "show", interface],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"nsenter ip link show {interface} failed for PID {pid} "
        f"(exit {result.returncode}):\n{result.stderr}"
    )
    return result.stdout


def test_create_veth_between_container_namespaces(
    require_root: None, live_env: LiveEnvironment
) -> None:
    """Production ``create_veth`` wires two namespaces and restores the host."""
    docker_client = live_env.docker
    base_name = unique_project_name("satgonetem-live-root")
    names = [f"{base_name}-a", f"{base_name}-b"]
    containers = []
    try:
        for name in names:
            containers.append(
                docker_client.containers.run(
                    "alpine:3.20",
                    ["sleep", "120"],
                    name=name,
                    network_mode="none",
                    detach=True,
                    remove=False,
                )
            )
        pids = [_wait_for_running(container) for container in containers]

        host_namespace = os.stat("/proc/self/ns/net").st_ino
        LinuxNetworkNamespace().create_veth(
            pids[0], pids[1], _SOURCE_INTERFACE, _DESTINATION_INTERFACE
        )

        for pid, interface in zip(
            pids, (_SOURCE_INTERFACE, _DESTINATION_INTERFACE), strict=True
        ):
            output = _nsenter_ip_link(pid, interface)
            assert interface in output, (
                f"interface {interface} is missing from PID {pid}:\n{output}"
            )
            assert "state UP" in output, (
                f"interface {interface} in PID {pid} is not up:\n{output}"
            )

        assert os.stat("/proc/self/ns/net").st_ino == host_namespace, (
            "create_veth did not restore the host network namespace"
        )
        host_links = subprocess.run(
            ["ip", "link", "show"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert host_links.returncode == 0, (
            "host namespace is unusable after create_veth:\n" + host_links.stderr
        )
    finally:
        cleanup_errors = []
        for container in containers:
            try:
                container.remove(force=True)
            except Exception as exc:  # noqa: BLE001 - collect every failure
                cleanup_errors.append(f"could not remove {container.name}: {exc!r}")
        if cleanup_errors:
            raise RuntimeError(
                "root namespace cleanup failed:\n"
                + "\n".join(f"  - {error}" for error in cleanup_errors)
            )
