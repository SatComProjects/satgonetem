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
| `gonetem_launcher.py` | `GoNetEmLauncher`, project generation, gRPC calls |
| `hil_manager.py` | `HILManager` (standalone, not a `NetworkLauncher`) |

`satgonetem.launchers` re-exports only `HILManager`. Import the GoNetEm
backend explicitly:

```python
from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher
```

## `NetworkLauncher` contract

`NetworkLauncher` is an `abc.ABC` with a shared constructor and six abstract
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
| `start_containers(nodes, workers=64, progress_cb=None)` | Provision containers/processes |
| `wire_links(links, workers=64, progress_cb=None)` | Create veths/bridges and apply initial qdiscs |
| `close_project(progress_cb=None)` | Tear down all containers and links |

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
5. Attach docker-py `Container` objects and PIDs to nodes, and set
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

The generated YAML uses `jitter: 0` and no loss. `_link_request()`, used for
dynamic updates, forwards `link.loss` (default 0) and `link.jitter` (default 0)
when present.

### Dynamic link operations

| Method | RPC | Notes |
|---|---|---|
| `update_link(link)` | `LinkUpdate` | Logs and swallows errors; does not raise |
| `update_links(links)` | `LinkUpdateMany` | Chunks of `LINK_UPDATE_CHUNK_SIZE = 512`; returns a merged response; preserves `grpc.RpcError` for `UNIMPLEMENTED` fallback |
| `add_link(link)` | `LinkAdd` | Logs and swallows errors |
| `delete_link(link)` | `LinkDel` | Logs and swallows errors |
| `set_link_capacities(...)` | `LinkUpdate` per link | Updates non-budget links and pushes every link |

`update_links()` raises `RuntimeError` if the project is not open or the
response count does not match the request. `bulk_link_operations()` in
`LinkOpsMixin` uses `update_links()` when available and falls back to
per-link `update_link()` on servers that report `UNIMPLEMENTED`.

### Teardown

`close_project()` streams `ProjectClose`, reports progress, closes the gRPC
channel, and clears the client. It is a no-op if no project is open.

`force_close_project()` is the recovery path:

1. List Docker containers with `docker ps -a`.
2. Keep only names equal to `ntm<project_id>` or starting with
   `ntm<project_id>.` (the exact project prefix; a bare `ntm` filter would
   match every project on the host).
3. `docker stop -t 0` and `docker rm` those containers.
4. Call `ProjectClose` and close the channel.

It is used by `start_gonetem()` after a failed launch and by
`TopologyManager.force_stop_gonetem()`. When the project ID is empty,
container cleanup is skipped rather than risking a host-wide match.

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
#   launcher = GoNetEmLauncher(...)
#   nodes = all nodes minus HIL-managed ones
#   links = active links minus HIL-managed ones
#   launcher.start_containers(nodes, links=links)
#   hil.wire_links(hil_links)  # if a HIL manager is configured
```

The manager stores the active launcher on `topology.launcher` and resets it
on teardown. `NetworkConfig.network_launcher` is currently metadata: the
lifecycle always constructs `GoNetEmLauncher` and does not branch on that
string. A custom backend therefore requires overriding `start_gonetem()` (or
driving `NetworkLauncher` yourself); merely setting
`network_launcher="MY_BACKEND"` has no effect in this version.

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
