"""
GoNetem gRPC-based network launcher.

Delegates all container/link management to a running GoNetem server via gRPC.
Uses the synchronous grpc channel so every method is blocking and thread-safe (no sense doing async, thought it through).

Also owns project file generation (get_gonetem_topology / generate_project)
since it is the only launcher that needs the GoNetem .gnet format.
"""

import grpc

import satgonetem.proto.netem_pb2_grpc as netem_grpc
from satgonetem.launchers.base_launcher import NetworkLauncher
from satgonetem.launchers.gonetem_cleanup import GoNetEmCleanupMixin
from satgonetem.launchers.gonetem_lifecycle import (
    LINK_UPDATE_CHUNK_SIZE as LINK_UPDATE_CHUNK_SIZE,
)
from satgonetem.launchers.gonetem_lifecycle import (
    GoNetEmLifecycleMixin,
)
from satgonetem.launchers.gonetem_project import (
    GoNetEmProjectBuilder,
)
from satgonetem.launchers.gonetem_project import (
    _tbf_qos as _tbf_qos,
)
from satgonetem.ports import (
    CommandRunner,
    ContainerRuntime,
    DockerContainerRuntime,
    GoNetEmGrpcClientFactory,
    GrpcClientFactory,
    default_runner,
)
from satgonetem.proto import netem_pb2


class GoNetEmLauncher(GoNetEmLifecycleMixin, GoNetEmCleanupMixin, NetworkLauncher):
    """Launch and manage a network topology through a GoNetem gRPC server."""

    def __init__(
        self,
        topology_manager,
        server_address: str,
        project_name: str,
        isl_capacity_kbps: int = 100_000,
        gnd_capacity_kbps: int = 100_000,
        ground_object_capacity_kbps: int = 100_000,
        temp_path: str = "/tmp/",
        netem_limit: int = 1000,
        txqueuelen: int = 1000,
        client_factory: GrpcClientFactory | None = None,
        command_runner: CommandRunner | None = None,
        container_runtime: ContainerRuntime | None = None,
    ) -> None:
        super().__init__(
            project_name,
            isl_capacity_kbps,
            gnd_capacity_kbps,
            ground_object_capacity_kbps,
            netem_limit,
            txqueuelen,
        )
        self._topology_manager = topology_manager
        self._server_address = server_address
        self._temp_path = temp_path

        self._channel: grpc.Channel | None = None
        self._client: netem_grpc.NetemStub | None = None
        self._request: netem_pb2.ProjectRequest | None = None  # type: ignore[name-defined]
        self._client_factory = client_factory or GoNetEmGrpcClientFactory()
        self._connection = None
        self._command_runner = command_runner or default_runner
        self._container_runtime = container_runtime or DockerContainerRuntime()
        self._project_builder = GoNetEmProjectBuilder(self)

    # Project file generation

    def get_gonetem_topology(self, *args, **kwargs) -> str:
        return self._project_builder.get_gonetem_topology(*args, **kwargs)

    def generate_project(self, *args, **kwargs) -> str:
        return self._project_builder.generate_project(*args, **kwargs)
