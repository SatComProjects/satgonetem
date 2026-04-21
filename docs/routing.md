# Routing

## Architecture

Every routing protocol in satgonetem is implemented as a **RoutingDaemon** subclass.
The topology manager holds a single active daemon in `self.routing_daemon` and drives
it through three lifecycle methods: `init`, `update`, and `remove`.

```
TopologyManager
    |
    +-- routing_daemon: RoutingDaemon  (one of the classes below, or a custom one)
            |
            +-- StaticRoutingDaemon    ("static")
            +-- OSPFDaemon             ("dynamic-ospf")
            +-- SRMPLSDaemon           ("sr-mpls")
            +-- <YourDaemon>           ("your-method-name")
```

The topology manager knows nothing about the internal routing logic. All state
(label tables, routing tables, graph snapshots) lives inside the daemon itself.

---

## Built-in routing methods

| Config name       | Daemon class          | Description                                     |
|-------------------|-----------------------|-------------------------------------------------|
| `static`          | `StaticRoutingDaemon` | Dijkstra shortest-path, applied via `ip route`  |
| `dynamic-ospf`    | `OSPFDaemon`          | Bird2 OSPF, config written per node             |
| `dynamic-isis`    | (FRR IS-IS)           | FRR IS-IS, configured via vtysh                 |
| `sr-mpls`         | `SRMPLSDaemon`        | Segment Routing with MPLS label stacks          |

The active method is selected by `routing_method` in `config.yaml`, or by passing
`routing_method` to `topology.init_routing()`.

---

## Daemon interface

Every daemon must extend `satgonetem.routing.base_daemon.RoutingDaemon` and
implement three methods:

```python
from satgonetem.routing.base_daemon import RoutingDaemon

class MyDaemon(RoutingDaemon):

    def init(self, max_workers: int = 4) -> bool:
        """Called once when routing is enabled.

        Install initial routes on all nodes. Return True on success, False on
        failure (the topology manager will abort and return -1.0 to the caller).
        """

    def update(self, new_links: list, max_workers: int = 4) -> None:
        """Called every simulation time step and after link changes.

        Recompute and re-apply routes as needed. new_links contains any links
        that were added since the last call; the topology graph can be fetched
        via self.topology.get_current_graph().
        """

    def remove(self, node=None, max_workers: int = 4) -> None:
        """Called when routing is torn down.

        Delete all installed routes. If node is not None, remove routes only
        from that node; otherwise clean up all nodes.
        """
```

`self.topology` is set automatically by the base class and points to the
`TopologyManager` instance. Use it to access satellites, ground stations, and
the current graph - but do not store routing state on it; keep all state on
the daemon itself.

### Useful topology accessors

```python
self.topology.satellites          # Dict[int, Satellite]
self.topology.ground_stations     # Dict[int, GroundStation]
self.topology.get_current_graph() # networkx Graph with weight edges
self.topology.get_satellites()    # Iterator over Satellite objects
self.topology.get_ground_stations() # Iterator over GroundStation objects
self.topology.status              # True once containers are running
self.topology.gonetem_is_on       # True once the network emulator is active
```

---

## Adding a custom routing method

No library modifications are required. Register your daemon class before
constructing `TopologyManager`:

```python
from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.services.topology_satcom import TopologyManager


class GreedyDaemon(RoutingDaemon):
    """Example: install routes greedily based on link capacity."""

    def init(self, max_workers: int = 4) -> bool:
        self._install_all_routes()
        return True

    def update(self, new_links: list, max_workers: int = 4) -> None:
        self._install_all_routes()

    def remove(self, node=None, max_workers: int = 4) -> None:
        targets = (
            [node]
            if node is not None
            else list(self.topology.satellites.values())
                + list(self.topology.ground_stations.values())
        )
        for n in targets:
            if n.container:
                n.container.exec_run(
                    ["sh", "-lc", "ip route flush table main 2>/dev/null; true"],
                    detach=False,
                )

    def _install_all_routes(self) -> None:
        graph = self.topology.get_current_graph()
        # ... compute and apply routes


# Register before constructing the manager
TopologyManager.register_routing_daemon("greedy", GreedyDaemon)

# Now "greedy" can be used as the routing method
topology = TopologyManager(project_path=my_path)
topology.init_routing(routing_method="greedy")
```

Alternatively, set `routing_method: greedy` in `config.yaml` and call
`topology.init_routing()` without arguments - the registered daemon will be
picked up automatically.

### Registration rules

- The name must not conflict with any built-in method name.
- The class must be a direct or indirect subclass of `RoutingDaemon`.
- Registration must happen before `TopologyManager.__init__` is called (or before
  `init_routing()` is called if the topology is already constructed).
- `register_routing_daemon` is a class method, so it affects all future instances.

---

## Lifecycle sequence

```
TopologyManager.__init__()
    -> routing_daemon = None

init_routing(routing_method="X")
    -> routing_daemon = XDaemon(topology)
    -> routing_daemon.init(max_workers)        # installs initial routes
    -> routing_initiated = True

[simulation runs, topology changes happen]

update_routing(new_links, max_workers)
    -> routing_daemon.update(new_links, max_workers)  # recomputes routes

rebuild_routing_for_current_timestep()
    -> routing_daemon.update([], max_workers)  # called every time step

delete_routing()
    -> routing_daemon.remove(max_workers=...)  # removes all routes
    -> routing_daemon = None
    -> routing_initiated = False
```

---

## Example: minimal latency-aware daemon

```python
import networkx as nx
from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.services.topology_satcom import TopologyManager


class LatencyDaemon(RoutingDaemon):
    """Install shortest-latency IP routes using a custom weight function."""

    def init(self, max_workers: int = 4) -> bool:
        try:
            self._apply_routes()
            return True
        except Exception as e:
            import logging
            logging.error(f"LatencyDaemon init failed: {e}")
            return False

    def update(self, new_links: list, max_workers: int = 4) -> None:
        self._apply_routes()

    def remove(self, node=None, max_workers: int = 4) -> None:
        targets = (
            [node] if node is not None
            else list(self.topology.satellites.values())
                + list(self.topology.ground_stations.values())
        )
        for n in targets:
            if n.container:
                n.container.exec_run(
                    ["ip", "route", "flush", "table", "main"], detach=False
                )

    def _apply_routes(self) -> None:
        graph = self.topology.get_current_graph()
        for gs_dst in self.topology.get_ground_stations():
            paths = nx.single_source_dijkstra_path(
                graph, gs_dst.id, weight="latency"
            )
            for src_id, path in paths.items():
                if src_id == gs_dst.id or len(path) < 2:
                    continue
                # resolve interface and install route ...


TopologyManager.register_routing_daemon("latency", LatencyDaemon)
```
