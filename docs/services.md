# TopologyManager Internals

`TopologyManager` is assembled from nine mixins, each owning one concern. The
public API is listed in [TopologyManager public API](topology_manager_api.md);
this document explains how the pieces fit together, the order in which they
run, and where to hook in.

Use this page when you need to:

- understand what happens on one simulation tick;
- debug ordering or state-flag problems;
- write a custom routing daemon or connection strategy;
- decide which mixin method to call for a lifecycle operation.

## Composition

```python
class TopologyManager(
    TopologySyncMixin,          # initial sync and per-tick topology update
    LinkOpsMixin,               # add/update/delete links + stats
    InterfaceMgrMixin,          # IPv4 plan, interfaces, address push
    NetworkLifecycleMixin,      # start/stop/force-stop, fast_start
    SimulationLoopMixin,        # background tick loop, lock, time jumps
    RoutingManagerMixin,        # daemon selection and lifecycle
    TrafficTestingMixin,        # ping/iperf3/hping3 fan-out
    DiagnosticsMixin,           # monitoring, tcpdump, terminals, summaries
    ControllerMixin,            # out-of-band management plane
):
    ...
```

All mixins assume they are composed onto an object exposing the shared state
described below. Several use `getattr`/`setdefault`, so lightweight test
harnesses can compose a subset of the mixins without calling
`TopologyManager.__init__`.

## `NetworkConfig`

`NetworkConfig` is a dataclass passed to the constructors. Every field has a
default; `TopologyManager` copies the values onto instance attributes.

```python
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

config = NetworkConfig(
    update_time=1,
    routing="static",
    isl_link_capacity=50_000,
    gnd_link_capacity=100_000,
    gonetem_server="localhost:10110",
)
topology = TopologyManager.from_file("topology_files/iridium_topology.json")
```

### General settings

| Field | Default | Meaning |
|---|---|---|
| `project_name` | `None` | Base project name; falls back to the simulation manager's name or `"Constellation"`. A Unix timestamp is appended for uniqueness |
| `update_time` | `5` | Tick interval in seconds |
| `protocol` | `"ipv4"` | Network-layer protocol label |
| `routing` | `"static"` | Default routing method |
| `satellite_image` | `"jariassuarez/sgnt:satellite"` | Satellite container image |
| `network_launcher` | `"GONETEM"` | Recorded backend identifier; see the launcher caveat below |
| `gonetem_server` | `"localhost:10110"` | GoNetEm gRPC endpoint |
| `use_budget` | `False` | Enable link-budget capacity computation for ground-station links |

### Link capacities and queues

| Field | Default | Unit |
|---|---|---|
| `gnd_link_capacity` | `100000` | kbps |
| `isl_link_capacity` | `100000` | kbps |
| `ground_object_link_capacity` | `100000` | kbps |
| `link_netem_limit` | `1000` | packets; `0` = server default |
| `link_txqueuelen` | `1000` | packets; `0` = server default |

`link_netem_limit` and `link_txqueuelen` are validated in `__post_init__`:
they must be integers (not bools) in `[0, 1_000_000]`. The upper bound matches
the GoNetEm server's `maxQueuePackets`.

### Data-plane addressing

| Field | Default |
|---|---|
| `ground_station_pool` | `10.0.0.0/10` |
| `satellite_pool` | `10.128.0.0/9` |
| `user_terminal_pool` | `10.64.0.0/10` |
| `ground_station_prefix_len` | `20` |
| `satellite_prefix_len` | `27` |
| `user_terminal_prefix_len` | `20` |

See [IPv4 Addressing](addressing.md) for allocation rules.

### Controller plane

| Field | Default |
|---|---|
| `controller_veth_host` | `"veth-host"` |
| `controller_veth_peer` | `"veth-controller"` |
| `controller_host_ip` | `"248.0.0.2"` |
| `controller_subnet_prefix` | `16` |
| `controller_bridge_name` | `"Controller"` |
| `controller_node_iface_pattern` | `"eth{node_id}"` |

See [Controller Plane](controller.md) for the full workflow.

> **Launcher caveat:** `network_launcher` is stored and serialized, but
> `start_gonetem()` currently always constructs `GoNetEmLauncher`. Setting
> this field to another value does not select a different backend. See
> [Network Launchers](launchers.md).

## Constructors and persistence

| Entry point | Use |
|---|---|
| `TopologyManager.from_satcom(project, network_config=None)` | Build from a `SimulationProperty` in memory (no YAML files) |
| `TopologyManager.from_simulation_manager(sm, network_config=None)` | Wrap an existing `SimulationManager` |
| `TopologyManager.from_file(path)` | Load a JSON file written by `to_file()` |
| `topology.to_file(path, network_config=None)` | Persist a reproducible topology |

`from_satcom()` calls `create_and_load_simulation()` and constructs the
manager, which immediately runs the initial sync. `from_file()` also restores
embedded ground-object CSV files into a temporary directory and rewrites the
`data_file` paths before building the project. Files produced from a live
`SimulationManager` include a `runtime_state` section so programmatically
added satellites and links survive a round trip.

### Registering a routing daemon

```python
from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.services.topology_satcom import TopologyManager

class MyDaemon(RoutingDaemon):
    def init(self, max_workers=4) -> bool: ...
    def update(self, new_links, max_workers=4) -> None: ...
    def remove(self, node=None, max_workers=4) -> None: ...

TopologyManager.register_routing_daemon("my-method", MyDaemon)
```

Registration is class-wide and must happen before construction. Built-in
names (`static`, `dynamic-ospf`, `dynamic-isis`, `sr-mpls`) cannot be
overridden and raise `ValueError`. A custom name that was already registered
is replaced by the new class. The daemon class must subclass `RoutingDaemon`
or `TypeError` is raised.

## Initialization order

`TopologyManager.__init__` does, in order:

1. Store `simulation_manager`, check that its time manager has a tick action.
2. Apply `NetworkConfig` to instance attributes.
3. Create `NetworkXAdapter`.
4. Initialize empty `satellites`, `ground_stations`, `links`, `interfaces`.
5. Initialize time state, flags, locks, and the routing/strategy registries.
6. Run `init()`:
   1. `_sync_satellites()` — build `Satellite` wrappers in parallel.
   2. `_sync_ground_stations()` — build `GroundStation` wrappers.
   3. `_sync_links()` — build `Link` wrappers and mark stale links.
   4. `_initialize_address_plan()` — build the topology-wide IPv4 plan and
      assign loopbacks.
   5. `_assign_interfaces_to_nodes()` — create the two interfaces for each
      link and assign `/31` addresses.
   6. `_add_loopback_interfaces_to_list()` — add loopbacks to `interfaces`.
   7. `check_for_updates()` — seed the change-detection hashes.

The plan is built before containers start. No command is sent to a container
during initialization; `set_ip_addresses()` pushes the planned addresses
later.

## Shared state

| Attribute | Meaning |
|---|---|
| `satellites` | `dict[int, Satellite]` keyed by numeric ID |
| `ground_stations` | `dict[int, GroundStation]` keyed by numeric ID |
| `links` | `dict[frozenset[str], Link]` keyed by the two node names |
| `interfaces` | Flat list of every `Interface`, including loopbacks |
| `interface_version` | Incremented whenever interfaces are added or removed |
| `address_plan` | `Ipv4AddressPlan` or `None` before init |
| `status` | Simulation manager is live/usable |
| `gonetem_is_on` | Backend project is open |
| `routing_initiated` | `init_routing()` succeeded and routes are installed |
| `running` | Background simulation loop is active |
| `current_time_step` | Number of ticks applied since the start |
| `initial_sync` | Internal first-sync marker |
| `launcher` | Active `NetworkLauncher` or `None` |
| `hil_manager` | Optional `HILManager` |
| `project_id`, `client`, `request` | Set by the launcher after startup |
| `last_simulation_error` | Most recent loop-tick exception |
| `_simulation_lock` | `RLock` serializing topology mutations |
| `_lifecycle_lock` | `Lock` serializing start/stop |

Status flags are not independent: `stop_gonetem()` clears `status`,
`gonetem_is_on`, and `routing_initiated`, and resets `routing_method` and
`launcher`.

## Locking model

One reentrant lock, `_simulation_lock`, serializes **every** topology
mutation:

- the background loop tick;
- `next_step()`;
- `move_to_time()`;
- `simulate_link_failure()`.

API mutations block until an in-flight tick finishes; they are not queued or
rejected. The lock is reentrant because `next_step()` and `move_to_time()`
call `update_simulation()`, which acquires it again. It is deliberately
separate from `_lifecycle_lock`, which `stop()` holds while joining the loop
thread; using one lock would deadlock the joining thread.

## Per-tick pipeline

`update_simulation(advance_timestep=True)` is the tick body. It runs while
holding `_simulation_lock`:

1. **Advance time** — increment `current_time_step` and call
   `time_manager.tick(update_time)`. The sat_com tick action updates the
   simulation manager's objects.
2. **Sync positions** — copy satellite and ground-station positions from
   `satcom_object`.
3. **Sync links** — one pass over `simulation_manager.get_all_links()`:
   - create missing `Link` wrappers;
   - refresh `satcom_object`, `is_active`, `to_add`/`to_remove`;
   - compute distances in a vectorised batch;
   - mark links missing from the simulation manager for removal.
4. **Delete stale links first** — HIL handovers reuse bridge/interface names,
   so the old link must be gone before the replacement is created.
5. **Apply additions and updates** — through `bulk_link_operations()` (the
   gRPC backend) and `_perform_local_link_operations()` (interface objects).
6. **Refresh routing** — only when routing was initialized and something
   changed. The daemon's `update()` receives the newly added links.
7. **Sync interface state** — copy each link's `is_active` onto its two peer
   interfaces.

A tick that raises is logged with its traceback, stored in
`last_simulation_error`, and retried on the next tick; the loop does not die.
Read the error with `get_last_simulation_error()`.

`next_step()` is the synchronous equivalent of one loop iteration and returns
the elapsed seconds (or `0.0` for a non-positive measurement).

## Lifecycle

Recommended order:

```python
topology = TopologyManager.from_file("topology_files/iridium_topology.json")
topology.start_gonetem()        # containers + links
topology.set_ip_addresses()     # push planned IPv4 addresses
topology.init_routing("static") # install routes
topology.start()                # background tick loop
...
topology.stop()                 # stop the loop
topology.stop_gonetem()         # close the project
```

`start_gonetem(controller=False)`:

- returns `None` if already started;
- builds a `GoNetEmLauncher`;
- filters HIL-managed nodes and links out of the software launch;
- calls `start_containers()` then `wire_links()` (a no-op for GoNetEm);
- wires HIL links through `hil_manager.wire_links()`;
- sets `status` and `gonetem_is_on`, records `start_time_`.

On failure it force-closes the project, clears the launcher, resets the three
flags, and re-raises.

`stop_gonetem()`:

- stops the loop if running;
- stops active traffic flows with a bounded wait (5 s per live thread);
- tears down HIL links;
- closes the launcher project;
- clears `launcher`, `status`, `gonetem_is_on`, `routing_initiated`, and
  `routing_method`.

It may raise a HIL teardown error after the rest of the teardown has
completed. `force_stop_gonetem()` follows the same sequence with
`force_close_project()`, emits a `ResourceWarning`, and is meant for recovery.

`set_ip_addresses(satellites=True, ground_stations=True)` requires
`address_plan` to exist. It calls `set_ipv4s_for_all_nodes()` and returns the
elapsed seconds.

`fast_start(routing_method="static")` is `start_gonetem()` +
`set_ip_addresses()` + `init_routing()` in one call. It does **not** start the
simulation loop.

## TopologySyncMixin

Public methods: `init()` and `update_simulation()`.

Internals worth knowing:

- **Link key** is `frozenset({"Sat0", "Gnd1"})`, independent of endpoint
  orientation. `Link.__eq__` uses the same rule.
- **Link activity** comes from the sat_com link's `is_active`, falling back to
  `not disabled`. A change sets `to_add` or `to_remove` accordingly.
- **Removal detection** compares the current key set with `self.links`; keys
  that disappeared are marked `to_remove`.
- **Distance refresh** uses `distance_3d_km_many()` for all real links in one
  batch, then `update_distance_and_delay()`.
- **Initial sync** creates wrappers only. New links are not marked `to_add`
  during the initial pass unless `initial_sync`/`add_anyway` is set; startup
  uses the full active link list instead of the flags.

## InterfaceMgrMixin

Owns the topology-wide address plan and interface construction.

- `_initialize_address_plan()` registers nodes in sorted-ID order, assigns
  loopbacks, and pre-allocates link slots in canonical order.
- `_build_interfaces_from_link()` is idempotent: a link that already has
  peer interfaces returns immediately.
- Interface types are inferred from node-name prefixes: `Gnd`–`Gnd` is
  `GroundObjectLink`, one `Gnd` is `GroundStationLink`, otherwise
  `InterSatelliteLink`.
- `set_ipv4s_for_all_nodes()` applies addresses per node in parallel, collects
  all failures, and raises one `RuntimeError` naming the failed nodes after
  every node has been attempted.
- `get_all_links_usage()` returns `[]` unless GoNetEm is running; otherwise it
  delegates to `monitor_links()`.

## LinkOpsMixin

`bulk_link_operations(to_add=True, to_update=True, to_del=True, ...)`
returns a statistics dictionary with these keys:

| Key group | Keys |
|---|---|
| Counts | `added_count`, `updated_count`, `deleted_count`, `update_error_count` |
| Add | `add_time_total`, `add_time_wall`, `add_time_per_link` |
| Update | `update_time_total`, `update_time_wall`, `update_time_per_link` |
| Delete | `delete_time_total`, `delete_time_wall`, `delete_time_per_link` |
| Side output | `links_to_add` |

Update semantics:

- Non-HIL links go through `LinkUpdateMany` in one batch; the server takes a
  single topology write lock.
- If the launcher lacks `update_links()` or the server reports
  `UNIMPLEMENTED`, updates fall back to per-link `update_link()`.
- Other gRPC errors propagate; a burst is never silently dropped.
- HIL links always use the per-link path and are handled by `HILManager`.

Add/delete run concurrently in a thread pool. After the backend operations,
`_perform_local_link_operations()` updates the local interface objects:
removals run sequentially (shared list mutation), add/update flag work runs in
parallel, and removed links are cleaned from `self.links`, `self.interfaces`,
and the address plan.

Local helpers such as `_execute_link_add/update/delete` dispatch to
`HILManager` when the link is HIL-managed and otherwise call the launcher.
They catch `RuntimeError`/`OSError` and log rather than raise.

## NetworkLifecycleMixin

Public methods: `start_gonetem`, `stop_gonetem`, `force_stop_gonetem`,
`set_ip_addresses`, `fast_start`.

Private helpers used by teardown:

- `_stop_simulation_loop()` — stop the loop if the thread is alive.
- `_stop_active_flows()` — call `stop()` on every running/pending flow and
  join its thread for at most 5 seconds.

Teardown must not leave stale state: even when stop fails, the
`finally` blocks clear `launcher`, `status`, and `gonetem_is_on`.

## SimulationLoopMixin

Public methods:

| Method | Behaviour |
|---|---|
| `start()` | Create a daemon thread running `_loop`; no-op if already running; raises `RuntimeError` if no project is loaded |
| `stop()` | Set the stop event, clear `running`, join the thread |
| `next_step()` | Acquire the simulation lock and call `update_simulation()`; returns elapsed seconds |
| `speed_up()` / `speed_down()` | Multiply `update_factor` by 0.9 / 1.1, clamped to `[0.01, 10.0]` |
| `set_update_time(seconds)` | Clamp to at least 1 second |
| `move_to_time(new_time)` | Set the time manager, execute actions, recompute `current_time_step`, rebuild routing; all under the lock |
| `rebuild_routing_for_current_timestep()` | Call the active daemon's `update([])` when routing is initialized and the topology is running |
| `simulate_link_failure(links)` | Remove one or more links from the simulation manager, then refresh topology; all under the lock |
| `get_last_simulation_error()` | Return the most recent tick exception or `None` |

The loop period is `update_time × update_factor` seconds. It polls the stop
event in 0.2-second slices, so `stop()` is responsive even with long tick
intervals. A tick failure is recorded and retried; the thread only exits when
the stop event is set.

`simulate_link_failure()` accepts only sat_com link types
`InterSatelliteLink`, `GroundStationLink`, and `UserTerminalLink`; other
types raise `AttributeError`.

## RoutingManagerMixin

- `init_routing(max_workers=MAX_WORKERS, routing_method="")` tears down any
  existing daemon, instantiates the requested one (built-in or registered),
  calls `init()`, and stores it on `routing_daemon`. It returns elapsed
  seconds or `-1.0` on failure. A failed init never leaves a half-initialized
  daemon installed.
- `delete_routing()` calls `remove()` and clears the daemon and flags; returns
  `-1.0` if routing was not initialized.
- `get_path_between_nodes(source, target)` runs `networkx.shortest_path()` on
  `get_current_graph()` using the `weight` attribute. It is a diagnostic
  helper, not the active daemon's forwarding table.
- `get_allowed_routing_methods()` returns the built-ins plus registered names.

`get_current_graph()` rebuilds a NetworkX graph from the simulation manager,
drops edges whose link is inactive or whose sat_com link is disabled, and
assigns weights according to `self.preference`:

| Preference | Edge weight |
|---|---|
| `hops_prefer_ISLs` | 1, or 0.5 for ISLs |
| `hops_no_preference` | 1 |
| `latency_prefer_ISLs` (default) | distance in km, ×0.1 for ISLs |
| `latency_no_preference` | distance in km |

Unknown preferences fall back to `latency_no_preference` with a warning.

See [Routing](routing.md) for the daemon interface and built-in methods.

## TrafficTestingMixin

`ping()`, `run_iperf3()`, and `run_hping3()` accept one node or a list for
each endpoint, create the Cartesian product of flows, start each in its own
thread, and return immediately. Every started flow is appended to
`_active_flows`, which `stop_gonetem()` uses for bounded cleanup.

The flows must be joined or polled by the caller:

```python
flows = topology.ping(topology.get_ground_stations(), topology.get_satellites()[:2])
for flow in flows:
    flow.join()
    print(flow.results())
```

See [Traffic Utilities](traffic.md) for configuration and result parsing.

## Diagnostics

`DiagnosticsMixin` provides the operational surface:

| Method | Purpose |
|---|---|
| `get_topology_summary()` | Counts of satellites, ground stations, and links |
| `get_node_by_name(name)` / `get_node_by_id(id)` | Node lookup; `None` when absent |
| `get_node_ip_addresses(name)` / `get_node_ifaces(name)` | Planned addresses and named interfaces |
| `execute_command_on(node, command)` | Run a command and return `{"output", "error"}` |
| `monitor_links(debug=False)` | Read `/tmp/interfaces/<project>.<SatN>` and return per-link usage |
| `get_all_links_usage()` | `monitor_links()` when GoNetEm is running, else `[]` |
| `enable_tcpdump_on_satellites(...)` | Start `tcpdump -s 96 -w <dump_dir>/<sat>_<iface>.pcap` per interface |
| `disable_tcpdump_on_satellites(...)` | `pkill tcpdump` in the selected satellites |
| `is_running_tcpdump()` | Probe `pgrep tcpdump` on satellites with containers |
| `open_bash_terminal(node)` | Spawn a local terminal running the NodeExec bridge |
| `get_coverage_percentage(...)` | Delegates to `coverage_percentage_fast()` over current satellites |

`monitor_links()` expects interface-stat files written by `net_logger` under
`/tmp/interfaces/`, named `<project_name>.<SatN>`. Each file's **last line**
must be a JSON object mapping interface names to bits per second. The method
keeps the maximum value seen for each link, sets `link.tx`, and returns:

```python
[
    {"src": "Sat0", "dst": "Sat1", "value": 272974304,
     "type": "InterSatelliteLink"},
    ...
]
```

`debug=True` additionally logs the top 20 links by usage. Missing files are
ignored; malformed JSON is logged and skipped.

`open_bash_terminal()` requires `project_id` (set by the launcher) and one of
`gnome-terminal`, `xterm`, `konsole`, `alacritty`, or `kitty`. The bridge
module is documented in [Utilities](utils.md#interactive-terminal-bridge).

## ControllerMixin

The controller plane is an out-of-band management network and is independent
of the data-plane address plan. The mixin provides:

| Method | Purpose |
|---|---|
| `setup_controller_connection()` | Create the host↔OVS veth pair and attach the peer to `Controller` |
| `setup_controller_addressing()` | Assign controller-plane management addresses to every node |
| `build_controller_ip(node_id)` | Build a management IP from a node ID |
| `build_satellite_ip(id)` | Deprecated alias |
| `get_management_ip(node_id)` | Read back the planned IP |
| `get_management_interface(node_id)` | Read back the interface name |
| `cleanup_controller_connection()` | Delete the veth and OVS port |

The controller helpers shell out to `sudo ip` and `docker`/`ovs-vsctl`; they
require a running OVS container and host privileges. See
[Controller Plane](controller.md) for the complete workflow.

## Extension points

| Need | Mechanism |
|---|---|
| New routing protocol | Subclass `RoutingDaemon`, then `TopologyManager.register_routing_daemon()` |
| New ground-to-space selection policy | Register a sat_com connection strategy; see [Utilities](utils.md#custom-connection-strategies) |
| New network backend | Subclass `NetworkLauncher`; currently requires overriding `start_gonetem()` because the lifecycle hardcodes GoNetEm |
| New diagnostics | Add a mixin with access to the shared state, or call `execute_command_on()` / `open_bash_terminal()` |
| Custom link capacity | Set `Link.peer1_capacity` / `peer2_capacity` or enable link budget |

## Failure and cleanup rules

- A failed `start_gonetem()` force-closes the project and resets all startup
  flags.
- A failed `stop_gonetem()` still clears the launcher and flags; it re-raises
  the HIL teardown error after cleanup.
- A failed routing init removes the partially initialized daemon and returns
  `-1.0`.
- A failed tick is recorded and retried; it does not stop the loop.
- A failed address push raises after all nodes have been attempted.
- Link add/update/delete errors are logged and counted; they do not abort the
  tick.

Tests that exercise these paths live under `tests/services/`, especially
`test_simulation_loop*.py`, `test_link_ops.py`,
`test_network_lifecycle*.py`, and `test_routing_mgr_core08.py`.

## Related documentation

- [TopologyManager public API](topology_manager_api.md)
- [Data Model Layer](models.md)
- [IPv4 Addressing](addressing.md)
- [Routing](routing.md)
- [Traffic Utilities](traffic.md)
- [Network Launchers](launchers.md)
- [Controller Plane](controller.md)
- [Hardware-in-the-Loop Manager](hil_manager.md)
