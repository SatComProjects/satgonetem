# External Boundaries and Ports

The ports in `satgonetem/ports.py` isolate external systems from orchestration
and domain code. They are intentionally small; adapters wrap the real library
and tests substitute fakes.

| Port | Default adapter | Consumers |
|---|---|---|
| `CommandRunner` | `SubprocessRunner.run` | controller, diagnostics, HIL, launcher Docker cleanup |
| `ProcessRunner` | `SubprocessRunner.popen` | diagnostics terminal launch, P4 controller |
| `GrpcClientFactory` | `GoNetEmGrpcClientFactory` | GoNetEm launcher |
| `ContainerRuntime` | `DockerContainerRuntime` | GoNetEm launcher container attachment |

## Injection

- `GoNetEmLauncher(..., client_factory=..., command_runner=..., container_runtime=...)`
- `P4Daemon(..., process_runner=...)`
- `HILManager(..., command_runner=...)`
- `ControllerMixin` / `DiagnosticsMixin`: set `command_runner` or
  `process_runner` on the composed `TopologyManager`; otherwise the default
  adapter is used.
- Traits use `default_runner` only when no runner is injected, so existing
  constructors and scripts keep working.

Protocols return normal `subprocess.CompletedProcess`/`Popen` objects so
callers retain familiar error types (`CalledProcessError`, `TimeoutExpired`).
Adapters stay thin: no topology knowledge, no retries, no business logic.

## Remaining boundaries

- `models/node.py` delegates container exec/PID lookup and namespace/veth
  operations to `ContainerRuntime`/`NetworkNamespace`. Its GoNetEm `NodeRun`
  command path remains a narrow in-model adapter because `Node` is the public
  command surface.
- `utils/node_terminal_bridge.py` is itself a CLI adapter and owns its gRPC
  stream.
- Generated protobuf code remains generated and is not edited.
