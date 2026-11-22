# Network Launchers

A launcher owns the lifecycle of the emulated network: it starts node
containers, wires virtual links, applies link QoS, updates links on every
simulation tick, and tears everything down. SatGoNetEm currently ships one
concrete backend, `GoNetEmLauncher`, which delegates all of those operations to
a local `gonetem-server` over gRPC.

Hardware-in-the-loop links are **not** a launcher: they are handled by
`HILManager` and excluded from the software launch. See
[Hardware-in-the-Loop Manager](hil_manager.md).

## Package layout

| Module | Contents |
|---|---|
| `base_launcher.py` | `NetworkLauncher` abstract base class |
| `registry.py` | Backend registry used by `network_launcher` |
| `gonetem_launcher.py` | Thin composition of the GoNetEm mixins |
| `gonetem_client.py` | gRPC connection, link requests, capacities |
| `gonetem_lifecycle.py` | Start/wire/link/close orchestration |
| `gonetem_control.py` | Optional node/interface/snapshot control RPCs |
| `gonetem_cleanup.py` | Docker cleanup and force-close |
| `gonetem_project.py` | `network.yml` and `.gnet` generation |
| `hil_manager.py` | `HILManager` (standalone, not a `NetworkLauncher`) |

`satgonetem.launchers` re-exports only `HILManager`. Import the GoNetEm
backend explicitly:

```python
from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
```

## `NetworkLauncher` contract

`NetworkLauncher` is an `abc.ABC` with a shared constructor and eight abstract
methods.

```python
class NetworkLauncher(ABC):
    def __init__(
        self,
        project_name: str,
        isl_capacity_kbps: int = 100_000,
        gnd_capacity_kbps: int = 100_000,
        ground_object_capacity_kbps: int = 100_000,
        netem_limit: int = 1000,
        txqueuelen: int = 1000,
    ) -> None: ...
```

### Lifecycle methods

| Method | Purpose |
|---|---|
| `start_containers(nodes, workers=64, progress_cb=None, controller=False, links=None)` | Provision containers/processes; returns `(container_time, link_time)` |
| `wire_links(links, workers=64, progress_cb=None)` | Create veths/bridges and apply initial qdiscs |
| `close_project(progress_cb=None)` | Tear down all containers and links |
| `force_close_project()` | Best-effort teardown after a failed startup; returns whether cleanup completed |

### Per-tick link methods

| Method | Purpose |
|---|---|
| `update_link(link)` | Update delay and rate on both ends |
| `add_link(link)` | Create a link and apply qdiscs |
| `delete_link(link)` | Remove both ends of a link |
| `set_link_capacities(isl_kbps, gnd_kbps, links)` | Update default capacities and push them |

The progress callback signature is:

```python
def progress(event: str, current: int, total: int) -> None: ...
```

`workers` is a hint. `GoNetEmLauncher` ignores it because the GoNetEm server
manages its own worker pool.

## `GoNetEmLauncher`

### Constructor

```python
from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher

launcher = GoNetEmLauncher(
    topology_manager=topology,
    server_address="localhost:10110",
    project_name="MyConstellation",
    isl_capacity_kbps=100_000,
    gnd_capacity_kbps=100_000,
    ground_object_capacity_kbps=100_000,
    temp_path="/tmp/",
    netem_limit=1000,
    txqueuelen=1000,
)
```

| Argument | Meaning |
|---|---|
| `topology_manager` | Supplies `satellites`, `ground_stations`, `links`, and receives `project_id`/`client` after startup |
| `server_address` | GoNetEm gRPC endpoint; `NetworkConfig.gonetem_server` defaults to `localhost:10110` |
| `project_name` | Name sent to `ProjectOpen`; `TopologyManager` appends a Unix timestamp for uniqueness |
| `*_capacity_kbps` | Fallback capacities used when a link has no explicit `peer*_capacity` |
| `temp_path` | Directory for the temporary `project.gnet` archive |
| `netem_limit` | netem queue limit in packets (`0` = server default) |
| `txqueuelen` | Interface transmit queue length in packets (`0` = server default) |

The gRPC channel is created lazily and is **insecure** by design; GoNetEm is
expected to run on the same trusted host. The channel is closed by
`close_project()` and `force_close_project()`.

### Startup sequence

`start_containers()` performs the whole launch, including link creation, so
`wire_links()` is a no-op for this backend.

1. Open a gRPC channel and call `ServerGetVersion`; fail with a clear error if
   the server is unreachable.
2. Generate `project.gnet` from the current topology.
3. `ProjectOpen` with the archive bytes; store the returned project ID.
4. Consume the `TopologyRun` server stream, forwarding progress events.
5. Attach container handles and PIDs to nodes through `ContainerRuntime`, and set
   `node.grpc_client` / `node.project_id` for `NodeRun` execution.
6. Return `(container_time, link_time)` in seconds.

On failure, `_cleanup_failed_start()` best-effort force-closes the project and
closes the channel; the temporary archive is always removed.

### Progress events

| Event | When | `current` / `total` |
|---|---|---|
| `NODE_COUNT` | Node total known | `0` / node count |
| `LINK_COUNT` | Link total known | `0` / link count |
| `NODE_START` | Each node starts | started / nodes |
| `LINK_SETUP` | Each link is set up | set up / links |
| `NODE_LOADCONFIG` | Node configuration applied | configured / nodes |
| `COMPLETED` | Topology run finished | `0` / `0` |
| `CLOSE_NODE_COUNT`, `CLOSE_NODE_CLOSE`, `CLOSE_COMPLETED`, `CLOSE_ERROR` | Teardown | varies |

`TopologyManager.start_gonetem()` does not expose the callback. Use the
launcher directly if you need progress reporting.

### Generated project format

`get_gonetem_topology()` returns the text of `network.yml`:

```yaml
nodes:
 Sat0:
  type: docker.satellite
  image: jariassuarez/sgnt:satellite
  volumes:
  - /tmp:/tmp
links:
 - {peer1: Sat0.1, peer2: Sat1.0, peer1qos: {...}, peer2qos: {...}}
```

- Default node types are `docker.satellite` and `docker.gndstation`.
- `fallback=True` switches to `docker.host`, which uses the host network
  namespace instead of the project images.
- `controller=True` adds an `ovs` switch named `Controller`, a bridge, and one
  high-capacity, 1 ms link from every node to it.
- `nodes` and `links` can be filtered, which is how HIL-managed nodes and links
  are excluded.
- Satellite image defaults to `jariassuarez/sgnt:satellite`; ground-station
  images default to `jariassuarez/sgnt:ground-station`.
- Interface names in the YAML are `<node>.<peer_id>`, matching
  `Link.get_names()`.
- Node names, images, and link peer names are emitted as YAML scalars. Values
  that would change meaning in the flow mappings used for links (commas,
  brackets, braces, comments, newlines, YAML indicators, or type coercions)
  are double-quoted as JSON strings, which are valid YAML; ordinary values
  stay unquoted and byte-identical. Non-BMP characters are emitted literally
  rather than as UTF-16 surrogate escapes, which Go's YAML parsers reject.

`generate_project(dest_dir, ...)` wraps that YAML into a gzipped tar archive
named `project.gnet` with a single `network.yml` member. The filename is
random (a temporary file in `dest_dir`), so use the returned path.

### Link QoS

For each active link, the generated topology sets:

```text
delay = max(int(link.delay), 1) ms
rate  = link.get_capacity()          # peer1 capacity
limit = link_netem_limit
txqueuelen = link_txqueuelen
```

Both directions receive the same `rate` in the generated YAML. Per-direction
capacities (`peer1_capacity` / `peer2_capacity`) are used by later
`LinkUpdate`/`LinkUpdateMany` calls, so an asymmetric budget link can become
asymmetric only after the first dynamic update.

The helper `_tbf_qos(rate_kbps, delay_ms)` computes a burst of
`rate_bytes_per_sec / 250 + 1514` bytes and a buffer factor chosen so that
GoNetEm's `limit = buffer × rate × delay / 8` equals `burst + BDP`. Zero rate
returns `(0, 0.0)`.

The generated YAML uses `jitter: <link.jitter>` (default `0`) and emits a
`loss:` token only when `link.loss` is non-zero, so projects generated with
default links are byte-identical to earlier releases. `_link_request()`, used
for dynamic updates, forwards `link.loss` (default 0.0) and `link.jitter`
(default 0) to both peer QoS mappings.

### Dynamic link operations

| Method | RPC | Notes |
|---|---|---|
| `update_link(link)` | `LinkUpdate` | Raises `RuntimeError` on transport or non-OK application status |
| `update_links(links)` | `LinkUpdateMany` | Chunks of `LINK_UPDATE_CHUNK_SIZE = 512`; returns a merged response; preserves `grpc.RpcError` for `UNIMPLEMENTED` fallback |
| `add_link(link)` | `LinkAdd` | Raises `RuntimeError` on backend failure |
| `delete_link(link)` | `LinkDel` | Raises `RuntimeError` on backend failure |
| `set_link_capacities(...)` | `LinkUpdate` per link | Updates non-budget links and pushes every link |

`update_links()` raises `RuntimeError` if the project is not open or the
response count does not match the request. `bulk_link_operations()` in
`LinkOpsMixin` uses `update_links()` when available and falls back to
per-link `update_link()` on servers that report `UNIMPLEMENTED`.

### Backend controls

`GoNetEmControlMixin` (composed into `GoNetEmLauncher`) exposes optional
control RPCs for an open project. Every method requires a live `_client` and
`_request`; otherwise it raises `RuntimeError` with an actionable message.
Unary responses are checked for `Status.code == ERROR` and raise
`RuntimeError(status.error)`. Transport `grpc.RpcError`s are wrapped in
`RuntimeError` with the original exception as `__cause__`.

| Method | RPC | Notes |
|---|---|---|
| `restart_node(node_name)` | `NodeRestart` | Container restarts without addresses, routes, or qdiscs |
| `set_interface_state(node_name, if_index, up)` | `NodeSetIfState` | `if_index` is the `ethN` suffix |
| `stop_all_nodes()` | `TopologyStopAll` | Stops every node container |
| `start_all_nodes()` | `TopologyStartAll` | Containers start unconfigured |
| `capture_interface(node_name, if_index, timeout_s=30.0)` | `NodeCapture` | Returns pcap bytes; deadline always applied |
| `get_backend_status()` | `ProjectGetStatus` | Returns a JSON-serializable dictionary |
| `save_project(timeout_s=60.0)` | `ProjectSave` | Returns archive bytes; rejects an empty stream |

`CaptureSrvMsg` has no `DATA` code. The launcher treats `STDOUT` as capture
payload, ignores `STDERR` (diagnostics), requires `OK` as the completion marker
or a valid pcap/pcapng header, and raises on `ERROR`, an empty capture, or a
truncated clean EOF. Because `NodeCapture` is a live stream, the deadline bounds
it; if the deadline expires after non-empty payload arrived, the launcher returns
the partial pcap prefix instead of discarding it. This framing matches the
observed server behavior. `ProjectSave` only accepts `DATA` messages and ignores
`NODE_COUNT`/`NODE_SAVE` progress messages.

`BackendControlMixin` wraps these methods for `TopologyManager` (the
integrator composes it into the manager). It is deliberately optional:
`NetworkLauncher` does not declare new abstract methods, and each operation is
feature-detected on `self.launcher`, raising `RuntimeError` when the active
backend lacks it.

| Manager method | Behaviour |
|---|---|
| `restart_node(node_name)` | Calls the backend, then clears `node.container` / `node.container_pid`; the next reconfigure must reapply addressing, routing, and qdiscs |
| `set_interface_state(node_name, interface, up)` | Accepts an `Interface`, emulator name, or `ethN`; maps it to the integer `ifIndex` by requiring `eth<digits>` (loopbacks and malformed names are rejected) |
| `set_link_state(link, up)` | Sets `link.admin_up`, calls `NodeSetIfState` on both endpoints, and updates both peer interface flags; a later topology tick cannot overwrite the admin state because `update_interfaces_state()` uses `is_active and admin_up` |
| `stop_all_nodes()` | Stops a running simulation loop first (without holding the simulation lock across `stop()`), then calls `TopologyStopAll` |
| `start_all_nodes()` | Calls `TopologyStartAll`; callers must reapply addresses, routes, and qdiscs |
| `capture_interface(node_name, interface, output=None, timeout_s=30.0)` | Returns bytes and atomically writes `output` when provided |
| `get_backend_status()` | Validates that the backend result is a JSON-serializable dict |
| `save_snapshot(path)` | Calls `ProjectSave` and atomically replaces `path` via a temporary file |
| `set_link_impairment(link, loss_percent=None, jitter_ms=None)` | Updates `link.loss` / `link.jitter`, marks `to_update`, calls `update_links([link])`, and inspects both the batch status and the per-link result; on any failure it raises and leaves `to_update=True` for retry |

`set_link_impairment()` intentionally uses the verified `update_links()` batch
rather than the singular path. If the backend has no `update_links` capability,
the method raises rather than silently accepting an unverified impairment.
`add_link`/`delete_link`/`update_link` now raise on a non-OK
`AckResponse.status` and on a closed project; HIL setup/teardown return failure
so LinkOps keeps the operation pending for retry.

### Teardown

`close_project()` streams `ProjectClose`, reports progress, closes the gRPC
channel, and clears the client. It is a no-op if no project is open.

`force_close_project()` is the recovery path:

1. List Docker containers with `docker ps -a`.
2. Keep only names equal to `ntm<project_id>` or starting with
   `ntm<project_id>.` (the exact project prefix; a bare `ntm` filter would
   match every project on the host).
3. `docker stop -t 0` and `docker rm` those containers.
4. Call `ProjectClose`, drain the server-streaming response with a 10-second
   deadline (`FORCE_CLOSE_TIMEOUT_SECONDS`). Draining lets the server finish the
   close before the transport disappears. The RPC is skipped when the client was
   already cleared by a failed `close_project()`; container cleanup still runs
   from the retained project id. On success the channel is closed; a failed
   `ProjectClose` retains the transport so the RPC can be retried, while a
   Docker-only failure closes it because only container cleanup remains.

It is used by `start_gonetem()` after a failed launch and by
`TopologyManager.force_stop_gonetem()`. When the project ID is empty,
container cleanup is skipped rather than risking a host-wide match, and the
transport is closed because no retry can target a project. The method is
best-effort and returns `bool`: Docker and RPC failures are logged rather than
raised, and `False` means at least one cleanup step failed, so callers must not
assume the project closed cleanly. A failed `ProjectClose` retains the request
and transport for a retry; a Docker-only failure keeps the request (container
cleanup can be retried) but closes the transport. It returns `True` when cleanup
is confirmed, including when there was nothing to clean (no retained request or
no matching containers).

## Using the launcher directly

Generating a project file does not require a running server:

```python
from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher

launcher = GoNetEmLauncher(
    topology_manager=topology,
    server_address="localhost:10110",
    project_name="inspect",
)
archive = launcher.generate_project("/tmp", controller=False)
print(archive)
```

Starting it directly bypasses the HIL filtering and state flags that
`TopologyManager.start_gonetem()` performs, so prefer the manager-level API
unless you are debugging the backend.

## Relationship to `TopologyManager`

`TopologyManager.start_gonetem()` is the supported entry point:

```python
topology.start_gonetem()
# internally:
#   launcher = create_launcher(network_launcher, ...)
#   nodes = all nodes minus HIL-managed ones
#   links = active links minus HIL-managed ones
#   launcher.start_containers(nodes, links=links)
#   hil.wire_links(hil_links)  # if a HIL manager is configured
```

The manager stores the active launcher on `topology.launcher` and resets it
on teardown. `NetworkConfig.network_launcher` selects the backend through
`satgonetem.launchers.registry.create_launcher`; register a custom backend with
`register_launcher("MY_BACKEND", MyLauncher)`. GoNetEm remains the default and
is resolved dynamically for compatibility.

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| `cannot reach server ...` | `gonetem-server` is not running, or `gonetem_server` points at the wrong address |
| `ProjectOpen returned an empty project id` | Server version/protocol mismatch; check `gonetem-server --version` |
| `LinkUpdateMany is not implemented by the server` | Older server; the service layer falls back to per-link `LinkUpdate` |
| `could not attach container for SatN` | Docker name `ntm<project>.<node>` was not found; container startup may have failed, or the Docker socket is inaccessible |
| `NodeRun` fails with `UNIMPLEMENTED` | Older server; `Node.execute_command()` falls back to docker-py |
| `force_close_project` leaves containers | The project ID was empty, or Docker was unreachable; clean up `ntm*` containers manually |

Relevant tests: `tests/launchers/test_gonetem_launcher.py`,
`tests/launchers/test_gonetem_launcher_launch02.py`, and
`tests/services/test_network_lifecycle.py`.

## Related documentation

- [Hardware-in-the-Loop Manager](hil_manager.md)
- [TopologyManager public API](topology_manager_api.md)
- [TopologyManager internals](services.md)
- [GoNetEm gRPC Protocol](proto.md)
- [Link Budget](link_budget.md)
