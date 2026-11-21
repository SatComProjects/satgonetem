"""Narrow ports and default adapters for external boundaries.

The protocols in this module are deliberately small. Production code depends
on them instead of calling ``subprocess`` or creating gRPC stubs directly;
tests substitute fakes without patching library internals.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CommandRunner(Protocol):
    """Run a finite command and return its completed-process result."""

    def run(
        self, args: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]: ...


@runtime_checkable
class ProcessRunner(Protocol):
    """Start a long-running process and return the handle."""

    def popen(self, args: Sequence[str], **kwargs: Any) -> subprocess.Popen: ...


class SubprocessRunner:
    """Default adapter backed by :mod:`subprocess`."""

    def run(
        self, args: Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if not any(key in kwargs for key in ("capture_output", "stdout", "stderr")):
            kwargs["capture_output"] = True
        kwargs.setdefault("text", True)
        return subprocess.run(list(args), **kwargs)

    def popen(self, args: Sequence[str], **kwargs: Any) -> subprocess.Popen:
        return subprocess.Popen(list(args), **kwargs)


default_runner = SubprocessRunner()


@runtime_checkable
class GrpcConnection(Protocol):
    """A gRPC channel/stub pair that can be closed as one unit."""

    channel: Any
    client: Any

    def close(self) -> None: ...


class GrpcClientFactory(Protocol):
    """Create a GoNetEm gRPC connection for a server address."""

    def connect(self, server_address: str) -> GrpcConnection: ...


class _GrpcConnection:
    def __init__(self, channel: Any, client: Any) -> None:
        self.channel = channel
        self.client = client

    def close(self) -> None:
        self.channel.close()


class GoNetEmGrpcClientFactory:
    """Default adapter that creates an insecure GoNetEm channel and stub."""

    def connect(self, server_address: str) -> GrpcConnection:
        import grpc

        import satgonetem.proto.netem_pb2_grpc as netem_grpc

        channel = grpc.insecure_channel(server_address)
        return _GrpcConnection(channel, netem_grpc.NetemStub(channel))


class ContainerRuntime(Protocol):
    """Container lookup and command execution."""

    def get(self, name: str) -> Any: ...

    def pid(self, container: Any) -> int | None: ...

    def exec(
        self, container: Any, command: list[str], detach: bool = False
    ) -> tuple[int, Any]: ...


class DockerContainerRuntime:
    """Default container adapter backed by the Docker SDK."""

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def get(self, name: str) -> Any:
        return self._get_client().containers.get(name)

    def pid(self, container: Any) -> int | None:
        pid = container.attrs.get("State", {}).get("Pid")
        return int(pid) if pid else None

    def exec(
        self, container: Any, command: list[str], detach: bool = False
    ) -> tuple[int, Any]:
        return container.exec_run(cmd=command, detach=detach)


class NetworkNamespace(Protocol):
    """Create interfaces inside Linux network namespaces."""

    def create_dummy_interface(self, pid: int, name: str) -> None: ...

    def create_veth(
        self,
        source_pid: int,
        destination_pid: int,
        source_name: str,
        destination_name: str,
    ) -> None: ...


class LinuxNetworkNamespace:
    """Default namespace adapter using setns and pyroute2."""

    CLONE_NEWNET = 0x40000000

    @staticmethod
    def _setns(libc: Any, ns_fd: Any, pid: int) -> None:
        import ctypes

        if libc.setns(ns_fd.fileno(), LinuxNetworkNamespace.CLONE_NEWNET) != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"setns failed for PID {pid}")

    def create_dummy_interface(self, pid: int, name: str) -> None:
        import ctypes

        from pyroute2 import IPRoute

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        with open("/proc/self/ns/net", "rb") as saved_ns:
            try:
                with open(f"/proc/{pid}/ns/net", "rb") as ns_fd:
                    self._setns(libc, ns_fd, pid)
                    with IPRoute() as ipr:
                        ipr.link("add", ifname=name, kind="dummy")
                        idx = ipr.link_lookup(ifname=name)[0]
                        ipr.link("set", index=idx, state="up")
            finally:
                libc.setns(saved_ns.fileno(), self.CLONE_NEWNET)

    def create_veth(
        self,
        source_pid: int,
        destination_pid: int,
        source_name: str,
        destination_name: str,
    ) -> None:
        import ctypes

        from pyroute2 import IPRoute

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        with open("/proc/self/ns/net", "rb") as saved_ns:
            try:
                with open(f"/proc/{source_pid}/ns/net", "rb") as ns_fd:
                    self._setns(libc, ns_fd, source_pid)
                    with IPRoute() as ipr:
                        ipr.link(
                            "add",
                            ifname=source_name,
                            kind="veth",
                            peer={
                                "ifname": destination_name,
                                "net_ns_fd": f"/proc/{destination_pid}/ns/net",
                            },
                        )
                        idx = ipr.link_lookup(ifname=source_name)[0]
                        ipr.link("set", index=idx, state="up")

                with open(f"/proc/{destination_pid}/ns/net", "rb") as ns_fd:
                    self._setns(libc, ns_fd, destination_pid)
                    with IPRoute() as ipr:
                        idx = ipr.link_lookup(ifname=destination_name)[0]
                        ipr.link("set", index=idx, state="up")
            finally:
                libc.setns(saved_ns.fileno(), self.CLONE_NEWNET)


__all__ = [
    "CommandRunner",
    "ContainerRuntime",
    "DockerContainerRuntime",
    "GoNetEmGrpcClientFactory",
    "GrpcClientFactory",
    "GrpcConnection",
    "LinuxNetworkNamespace",
    "NetworkNamespace",
    "ProcessRunner",
    "SubprocessRunner",
    "default_runner",
]
