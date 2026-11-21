"""Narrow ports and default adapters for external boundaries.

The protocols in this module are deliberately small. Production code depends
on them instead of calling ``subprocess`` or creating gRPC stubs directly;
tests substitute fakes without patching library internals.
"""

from __future__ import annotations

import subprocess
from typing import Any, Protocol, Sequence, runtime_checkable


@runtime_checkable
class CommandRunner(Protocol):
    """Run a finite command and return its completed-process result."""

    def run(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ...


@runtime_checkable
class ProcessRunner(Protocol):
    """Start a long-running process and return the handle."""

    def popen(self, args: Sequence[str], **kwargs: Any) -> subprocess.Popen:
        ...


class SubprocessRunner:
    """Default adapter backed by :mod:`subprocess`."""

    def run(self, args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
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

    def close(self) -> None:
        ...


class GrpcClientFactory(Protocol):
    """Create a GoNetEm gRPC connection for a server address."""

    def connect(self, server_address: str) -> GrpcConnection:
        ...


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
    """Look up containers by name."""

    def get(self, name: str) -> Any:
        ...


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


__all__ = [
    "CommandRunner",
    "ContainerRuntime",
    "DockerContainerRuntime",
    "GoNetEmGrpcClientFactory",
    "GrpcClientFactory",
    "GrpcConnection",
    "ProcessRunner",
    "SubprocessRunner",
    "default_runner",
]
