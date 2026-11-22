# Launchers

A **launcher** manages the full lifecycle of the emulated network: starting containers, wiring virtual links, updating link parameters at each simulation step, and tearing everything down.

## Abstract interface — `NetworkLauncher`

All launchers inherit from `NetworkLauncher` ([base_launcher.py](base_launcher.py)) and must implement:

| Method | When called | Responsibility |
|---|---|---|
| `start_containers(nodes, workers, progress_cb, controller=False, links=None)` | Once, at project start | Start one container per node; attach `node.container` and `node.container_pid`; return `(container_time, link_time)` |
| `wire_links(links, workers, progress_cb)` | Once, after containers are up | Create veth pairs for every active link and apply initial netem/TBF qdiscs |
| `close_project(progress_cb)` | On project shutdown | Remove all containers and clean up network state |
| `update_link(link)` | Every simulation step, per changed link | Push new delay / rate to both ends of an existing link |
| `add_link(link)` | Every simulation step, per new link | Create veth pair + qdiscs for a newly visible link |
| `delete_link(link)` | Every simulation step, per removed link | Tear down the veth pair for a link that disappeared |
| `set_link_capacities(isl_kbps, gnd_kbps, links)` | On-demand | Bulk-update the default ISL / GSL capacity for all active links |
| `force_close_project()` | After a failed startup | Best-effort teardown; returns whether cleanup completed |

The constructor accepts `project_name`, the three capacity defaults, `netem_limit`, and `txqueuelen`.

## Implementation

### `GoNetEmLauncher` ([gonetem_launcher.py](gonetem_launcher.py))

The implementation is composed from `gonetem_client.py` (gRPC),
`gonetem_lifecycle.py` (start/wire/links/close), `gonetem_cleanup.py` (Docker
cleanup), and `gonetem_project.py` (YAML/.gnet generation).

gRPC-based backend that delegates all network operations to a running GoNetEm daemon.

## Creating a new launcher

1. Subclass `NetworkLauncher`.
2. Implement all eight abstract methods listed above.
3. In `start_containers`, set `node.container` and `node.container_pid` on every node — the rest of the system relies on these for `exec_run` calls (routing, FRR, monitoring, etc.).
4. Register the launcher with `register_launcher("MY_BACKEND", MyLauncher)` and set `network_launcher="MY_BACKEND"`.

```python
from satgonetem.launchers.base_launcher import NetworkLauncher


class MyLauncher(NetworkLauncher):
    def start_containers(
        self, nodes, workers=64, progress_cb=None, controller=False, links=None
    ):
        for node in nodes:
            node.container = ...  # container handle via ContainerRuntime
            node.container_pid = ...  # kernel PID (int)
        return (0.0, 0.0)

    def wire_links(self, links, workers=64, progress_cb=None): ...
    def close_project(self, progress_cb=None): ...
    def force_close_project(self) -> bool: ...
    def update_link(self, link): ...
    def add_link(self, link): ...
    def delete_link(self, link): ...
    def set_link_capacities(self, isl_kbps, gnd_kbps, links): ...
```
