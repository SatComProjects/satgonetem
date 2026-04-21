# Launchers

A **launcher** manages the full lifecycle of the emulated network: starting containers, wiring virtual links, updating link parameters at each simulation step, and tearing everything down.

## Abstract interface — `NetworkLauncher`

All launchers inherit from `NetworkLauncher` ([base_launcher.py](base_launcher.py)) and must implement:

| Method | When called | Responsibility |
|---|---|---|
| `start_containers(nodes, workers, progress_cb)` | Once, at project start | Start one container per node; attach `node.container` and `node.container_pid` to each node object |
| `wire_links(links, workers, progress_cb)` | Once, after containers are up | Create veth pairs for every active link and apply initial netem/TBF qdiscs |
| `close_project(progress_cb)` | On project shutdown | Remove all containers and clean up network state |
| `update_link(link)` | Every simulation step, per changed link | Push new delay / rate to both ends of an existing link |
| `add_link(link)` | Every simulation step, per new link | Create veth pair + qdiscs for a newly visible link |
| `delete_link(link)` | Every simulation step, per removed link | Tear down the veth pair for a link that disappeared |
| `set_link_capacities(isl_kbps, gnd_kbps, links)` | On-demand | Bulk-update the default ISL / GSL capacity for all active links |

The constructor accepts `project_name`, `isl_capacity_kbps`, and `gnd_capacity_kbps`.

## Implementations

### `DirectLauncher` ([direct_launcher.py](direct_launcher.py))

The production backend. Operates entirely without gRPC — uses Docker, pyroute2, and `nsenter + tc` directly:

- **Container startup** — `docker run --network none --privileged`; PIDs are batch-resolved via `docker inspect` and stored in `_node_pids`. `node.container` (docker-py object) and `node.container_pid` are set on each node so the rest of the system can `exec_run` commands.
- **Link wiring** — veth pairs are created by entering the source container's network namespace with `setns()` (one `libc` call per thread), then moved to the peer namespace with pyroute2. This keeps the number of `nsenter` round-trips to a minimum.
- **Qdisc setup** — netem (delay) + TBF (rate) are applied with a batched `tc -batch -` call via `nsenter`, one batch per container.
- **Per-step updates** — same batched `nsenter + tc` approach for delay/rate changes.

### `GoNetEmLauncher` ([gonetem_launcher.py](gonetem_launcher.py))

Legacy gRPC-based backend that delegates all network operations to a running GoNetEm daemon. Kept for compatibility.

## Creating a new launcher

1. Subclass `NetworkLauncher`.
2. Implement all seven abstract methods listed above.
3. In `start_containers`, set `node.container` and `node.container_pid` on every node — the rest of the system relies on these for `exec_run` calls (routing, FRR, monitoring, etc.).
4. Register the launcher in `projects.py` (`start_gonetem`) by instantiating your class instead of `DirectLauncher`.

```python
from satgonetem.launchers.base_launcher import NetworkLauncher

class MyLauncher(NetworkLauncher):

    def start_containers(self, nodes, workers=64, progress_cb=None):
        for node in nodes:
            # start container, then:
            node.container = ...       # docker-py Container object
            node.container_pid = ...   # kernel PID (int)

    def wire_links(self, links, workers=64, progress_cb=None): ...
    def close_project(self, progress_cb=None): ...
    def update_link(self, link): ...
    def add_link(self, link): ...
    def delete_link(self, link): ...
    def set_link_capacities(self, isl_kbps, gnd_kbps, links): ...
```
