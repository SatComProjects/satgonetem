# GoNetEm gRPC Protocol

SatGoNetEm talks to `gonetem-server` over gRPC. The protocol is defined in
[`satgonetem/proto/netem.proto`](../satgonetem/proto/netem.proto) and
consumed through the generated modules:

| File | Contents |
|---|---|
| `netem.proto` | Source of truth: service, messages, enums |
| `netem_pb2.py` | Generated message and enum classes |
| `netem_pb2_grpc.py` | Generated `NetemStub` and `NetemServicer` |

`satgonetem/proto/` has no `__init__.py` and works as a namespace package.
Do not edit the generated files by hand; change `netem.proto` and regenerate
both modules with a `grpcio-tools` version compatible with the runtime.

## Generated-code versions

The checked-in stubs were generated with:

- gRPC Python plugin **1.80.0** (`GRPC_GENERATED_VERSION`);
- protobuf **6.31.1** (`netem_pb2` runtime validation).

`netem_pb2_grpc.py` refuses to import if the installed `grpcio` is older than
the plugin version, and `netem_pb2.py` validates the protobuf runtime version.
The project declares `grpcio>=1.62.0` and `protobuf>=6.31.0`, so an environment
that satisfies the lower bounds but not the generated bounds will fail at
import time. Upgrade `grpcio` (and `grpcio-tools` if you regenerate) when
working on the protocol.

## Import layout

```python
from google.protobuf import empty_pb2
from satgonetem.proto import netem_pb2, netem_pb2_grpc
```

Importing `empty_pb2` first is intentional in several modules: it populates the
default descriptor pool before `netem_pb2` registers its own descriptors. This
avoids duplicate-descriptor errors in environments that load protobuf modules in
a different order.

## Connecting

```python
import grpc
from google.protobuf import empty_pb2

from satgonetem.proto import netem_pb2, netem_pb2_grpc

channel = grpc.insecure_channel("localhost:10110")
stub = netem_pb2_grpc.NetemStub(channel)

version = stub.ServerGetVersion(empty_pb2.Empty())
assert version.status.code == netem_pb2.OK
print(version.version)
```

`GoNetEmLauncher` owns this channel during normal operation; use the manager's
`topology.client` if you need the same stub. The channel is insecure because
the server is expected to be local/trusted.

## Service surface

`Netem` groups RPCs by object. Only the subset marked **used** is called by
SatGoNetEm today; the rest are available to tools that need them.

### Server

| RPC | Kind | Purpose |
|---|---|---|
| `ServerGetVersion` | unary | Version handshake (**used** by the launcher) |
| `ServerPullImages` | server stream | Pull every configured image |
| `ServerCleanContainers` | unary | Remove stale containers |

### Projects

| RPC | Kind | Purpose |
|---|---|---|
| `ProjectOpen` | unary | Open a `project.gnet` archive (**used**) |
| `ProjectClose` | server stream | Stop and remove the project (**used**) |
| `ProjectGetMany` | unary | List open projects |
| `ProjectSave` | server stream | Save a project archive |
| `ProjectGetNodeConfigs` | unary | Read per-node config files |
| `ProjectGetStatus` | unary | Project and node status |

`OpenRequest` carries `name` (display name) and `data` (the archive bytes).
`PrjOpenResponse` returns the opaque `id` used by every later
`ProjectRequest`.

### Topology

| RPC | Kind | Purpose |
|---|---|---|
| `TopologyRun` | server stream | Start nodes and wire links (**used**) |
| `TopologyReload` | server stream | Reload an existing topology |
| `TopologyCheck` | unary | Validate a topology |
| `TopologyStartAll` / `TopologyStopAll` | unary | Bulk node start/stop |
| `ReadNetworkFile` / `WriteNetworkFile` | unary | Read or replace `network.yml` |

`TopologyRunMsg` is the progress protocol:

| Code | Meaning |
|---|---|
| `NODE_COUNT`, `BRIDGE_COUNT`, `LINK_COUNT` | Totals |
| `NODE_START`, `BRIDGE_START`, `LINK_SETUP` | Per-object progress |
| `NODE_LOADCONFIG` | Node configuration applied |
| `NODE_MESSAGES` | Per-node log messages |
| `NODE_STOP`, `NODE_RM` | Teardown progress |

The launcher maps these to its own progress events
(`NODE_COUNT`, `LINK_COUNT`, `NODE_START`, `LINK_SETUP`, `NODE_LOADCONFIG`,
`COMPLETED`); see [Network Launchers](launchers.md).

### Nodes

| RPC | Kind | Purpose |
|---|---|---|
| `NodeRun` | unary | Run one command, return stdout/stderr/exit code (**used**) |
| `NodeExec` | bidirectional stream | Interactive TTY shell (**used**) |
| `NodeStart`, `NodeStop`, `NodeRestart` | unary | Container lifecycle |
| `NodeSetIfState` | unary | Bring an interface up/down |
| `NodeCapture` | server stream | Packet capture |
| `NodeCopyFrom`, `NodeCopyTo` | stream | File transfer |
| `NodeGetConsoleCmd` | unary | Console command for a node |
| `NodeReadConfigFiles` | unary | Read config files |
| `RouteInstall` | unary | Apply typed route batches (**used**) |

#### `NodeRun`

```python
request = netem_pb2.NodeRunRequest(
    prjId=project_id,
    node="Sat0",
    cmd=["sh", "-c", "ip -brief address"],
)
response = stub.NodeRun(request)
assert response.status.code == netem_pb2.OK
print(response.stdout.decode(), response.exitCode)
```

`Node.execute_command()` sends commands here when a gRPC client is attached
and the call is not detached. If the server returns `UNIMPLEMENTED`, it falls
back to docker-py. Non-zero exit codes raise unless listed in `ok_exit_codes`.

#### `NodeExec`

`ExecCltMsg` is client→server: `CMD`, `DATA`, `RESIZE`, `ERROR`, `CLOSE`.
`ExecSrvMsg` is server→client: `STDOUT`, `STDERR`, `ERROR`, `CLOSE`.

The terminal bridge opens a stream with:

```python
netem_pb2.ExecCltMsg(
    code=netem_pb2.ExecCltMsg.CMD,
    prjId=project_id,
    node=node_name,
    cmd=["/bin/bash"],
    tty=True,
)
```

and then forwards `DATA` and `RESIZE` messages. See
[Utilities](utils.md#interactive-terminal-bridge).

#### `RouteInstall`

`RouteInstallRequest` groups routes per node so one call installs many routes
with a bounded worker pool:

```python
request = netem_pb2.RouteInstallRequest(
    prjId=project_id,
    nodes={
        "Sat0": netem_pb2.RouteBatch(
            routes=[
                netem_pb2.RouteSpec(
                    action=netem_pb2.ROUTE_REPLACE,
                    destination="10.128.0.1/32",
                    gateway="10.128.0.5",
                    device="eth1",
                )
            ]
        )
    },
    maxConcurrency=16,
)
response = stub.RouteInstall(request)
```

`RouteSpec.destination` is CIDR; `gateway` may be empty for an on-link route;
`device` is the interface name inside the node. `RouteInstallResponse` returns
per-node `RouteInstallResult` values (applied count, stdout/stderr, exit code,
duration). `StaticRoutingDaemon` uses batches and falls back to per-node
`NodeRun` when the server reports `UNIMPLEMENTED`.

### Links

| RPC | Kind | Purpose |
|---|---|---|
| `LinkUpdate` | unary | Update one link's QoS (**used**) |
| `LinkUpdateMany` | unary | Update a burst under one topology lock (**used**) |
| `LinkAdd` | unary | Add one link (**used**) |
| `LinkDel` | unary | Delete one link (**used**) |

`LinkConfig` identifies the two ends with `peer1`/`peer2` names and carries
one `QoSConfig` per direction:

| Field | Type | Unit / note |
|---|---|---|
| `loss` | float | Packet loss fraction/percent as understood by GoNetEm |
| `delay` | int | Milliseconds |
| `jitter` | int | Milliseconds |
| `rate` | int | kbps |
| `buffer` | float | TBF buffer factor |
| `burst` | int | Bytes |
| `limit` | uint32 | netem queue limit in packets; `0` = server default |
| `txqueuelen` | uint32 | Interface queue length; `0` = server default |

`LinkUpdateManyRequest` takes a repeated `LinkConfig` and `sync`; the response
contains one `LinkUpdateResult` per link plus an `updated` count. SatGoNetEm
sends chunks of at most `LINK_UPDATE_CHUNK_SIZE = 512` and merges the
responses. The server applies the batch under a single topology write lock and
groups qdisc work by node.

## Status and error handling

Most unary responses embed:

```protobuf
message Status {
  StatusCode code = 1;   // OK = 0, ERROR = 1
  string error = 2;
}
```

Check `response.status.code` before using payload fields. Do not treat a
successful gRPC transport as success: a `Status.code == ERROR` response is an
application-level failure.

SatGoNetEm distinguishes three failure classes:

| Condition | Handling |
|---|---|
| `grpc.RpcError` with `UNIMPLEMENTED` | Feature-detect and fall back (LinkUpdateMany → LinkUpdate; RouteInstall → NodeRun; NodeRun → docker) |
| Other `grpc.RpcError` | Propagate or log, depending on the call site |
| `status.code == ERROR` | Raise `RuntimeError` with `status.error` |

Teardown paths log and continue where possible; startup paths clean up and
re-raise.

## Compatibility notes

- The protocol has no explicit version field. `ServerGetVersion` is the only
  handshake, and feature detection is done with `UNIMPLEMENTED`.
- Newer fields (`limit`, `txqueuelen`, `RouteInstall`, `LinkUpdateMany`) are
  additive. Older servers ignore them or return `UNIMPLEMENTED`.
- `ProjectClose` is a server stream even for small projects; consume it
  fully or the server may continue tearing down after the client returns.
- The generated code enforces its own gRPC/protobuf versions at import; this
  is separate from the server's protocol version.

## Related documentation

- [Network Launchers](launchers.md)
- [TopologyManager internals](services.md)
- [Routing](routing.md)
- [Utilities](utils.md)
