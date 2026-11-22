# Data Model Layer

`satgonetem.models` contains the runtime objects that represent the emulated
network: nodes, interfaces, links, routing entries, MPLS state, and the
sat_com configuration schema. `TopologyManager` owns these objects and mutates
them on every simulation tick, so they are the best place to inspect when
debugging topology state or writing a custom routing daemon.

The layer deliberately separates two kinds of objects:

- **Emulator objects** (`Node`, `Satellite`, `GroundStation`, `Interface`,
  `Link`) hold the state needed to drive containers, addresses, and `tc`
  qdiscs.
- **Orbital/original objects** (`satcom_object`) are the corresponding
  `sat_com_model` instances. They remain the source of truth for orbital
  position, link activity, and which links exist.

Each emulator object keeps a back-reference to its sat_com counterpart; the
`TopologySyncMixin` refreshes one from the other on every tick.

## Module map

| Module | Contents |
|---|---|
| `node.py` | `Node` base class: identity, interfaces, command execution |
| `satellite.py` | `Satellite(Node, QoSCapableMixin)` |
| `ground_station.py` | `GroundStation(Node, QoSCapableMixin)` |
| `interface.py` | `Interface`: name, IPv4 address/prefix, state, MAC |
| `link.py` | `Link`: endpoints, distance, delay, capacity, link-budget hook |
| `routing_entry.py` | `RoutingEntry`: one IPv4 route |
| `mpls_entry.py` | `MPLSConfig`, `SRNodeSIDEntry`, `SRForwardEntry`, `SRLabelStackEntry` |
| `mixins/qos_mixin.py` | `QoSCapableMixin`: TBF reconfiguration helper |
| `sat_com_models.py` | Pydantic mirror of the sat_com configuration schema |

The public names re-exported from `satgonetem.models` are `Node`, `Satellite`,
`GroundStation`, `Link`, `Interface`, `RoutingEntry`, `MPLSConfig`, and
`QoSCapableMixin`.

## Object graph

```
TopologyManager
├── satellites: dict[int, Satellite]        key = numeric node id
├── ground_stations: dict[int, GroundStation]
├── links: dict[frozenset[str], Link]       key = {"Sat0", "Gnd1"}
└── interfaces: list[Interface]             every interface in the topology
        │
        ├── Node.interfaces ── Interface.peer ── Interface
        ├── Node.loopback   ── Interface(/32)
        └── Link.peer_interfaces (0 or 2)
```

Numeric IDs are parsed from the name: `Sat0` → `0`, `Gnd12` → `12`. Names that
do not follow the three-letter prefix plus integer convention raise
`ValueError` during construction because `Node.__init__` evaluates
`int(name[3:])`.

## `Node`

`Node` is the base class for every addressable emulated node. It is not an
abstract base class in the `abc` sense; it can be instantiated, but
subclasses set `type` and satellite-specific state.

```python
from satgonetem.models import Node

node = Node("Sat7")
assert node.id == 7
# `type` is set by subclasses (Satellite/GroundStation), not by Node itself.
```

### Constructor state

| Attribute | Meaning |
|---|---|
| `name` | Node name, e.g. `"Sat7"` |
| `id` | Integer parsed from the name |
| `position` | `{"latitude", "longitude", "altitude"}`; altitude in km |
| `interfaces` | Interfaces on this node, including the loopback |
| `loopback` | `Interface("lo", iface_type="lo")`, assigned a `/32` by the address plan |
| `ipv4_routing_table` | `RoutingEntry` objects installed by the routing daemon |
| `ipv4_previous_routing_table` | Previous table used for diffing |
| `container` | Container handle attached by the launcher; accessed through `ContainerRuntime` |
| `container_pid` | Container PID used for netns operations |
| `grpc_client`, `project_id` | GoNetEm `NodeRun` context |
| `image` | Container image override |
| `antenna` | Optional `link_budget.antenna.Antenna` |
| `address_block` | `ipaddress.IPv4Network` assigned by the address plan |
| `command_output` | Stdout of the last `execute_command` call |

### Interface lifecycle

`create_interface(name)` appends an `Interface` to `node.interfaces` and
returns it. Link wiring uses two interfaces per link, named
`<node>.<peer_id>`:

```python
# On Sat0, the interface facing Sat1
iface = sat0.create_interface("Sat0.1")
assert iface.get_iname() == "eth1"
```

`remove_interface_connected_to_node(peer)` removes the interface whose
`peer.name` matches `<peer.name>.<self.id>`. It logs a warning if no match is
found and never raises for a missing interface.

`create_dummy_interface(name)` and `connect_to(peer, ...)` resolve container
PIDs and delegate namespace/veth work to the injected `NetworkNamespace`
adapter. Tests can replace `node.network_namespace`; the defaults use `setns`
and pyroute2.

### Command execution

`execute_command()` has two paths:

1. **GoNetEm gRPC `NodeRun`** when `node.grpc_client` is set, the command is
   not detached, and `use_docker_cli=False`.
2. **`ContainerRuntime.exec`** otherwise (including every detached command);
   the default adapter uses docker-py `exec_run`.

```python
stdout = node.execute_command("ip -brief address")
node.execute_command("tcpdump -i eth0 -w /tmp/dump.pcap", detach=True)
```

`ok_exit_codes` lets tools that return non-zero on a normal run (for example
`ping` with no replies) avoid raising. Detached commands always return `""`.
If `NodeRun` reports `UNIMPLEMENTED`, execution transparently falls back to
Docker.

`execute_command_docker()` remains public as the explicit Docker-only path.
`node.container_runtime` and `node.network_namespace` are injectable test
seams; no Docker or pyroute2 imports remain in `models/node.py`.

### Address application

`set_ipv4_to_containers(interface=None, set_lo=True)` batches all planned
addresses into one `ip -force -batch` invocation:

- One `addr replace <ip>/<prefix> dev ethN` line per interface with an address.
- One `addr replace <loopback>/32 dev lo` line when `set_lo=True`.
- Only `/31` link prefixes and `/32` loopback prefixes are accepted; anything
  else raises `ValueError`.

`set_ipv4s_for_all_nodes()` in `InterfaceMgrMixin` calls this method for every
node in parallel.

### Routing helpers

`get_routing_table()` returns `ip route show` split into lines (or `[]` when
the command fails). `add_policy_based_routing_rules()` installs an
`ip rule` per entry in `ipv4_routing_table`, using a deterministic rule ID
derived from the source/target node IDs. Subclasses may override it.

## `Satellite`

```python
from satgonetem.models import Satellite

sat = Satellite("Sat0")
sat.satcom_object = simulation_manager.get_satellites()[0]
sat.sync_position_from_satcom()
```

Adds:

| Attribute / method | Meaning |
|---|---|
| `addressable` | Whether the satellite receives a loopback route (see `set_addressable`) |
| `is_addressable()` / `set_addressable(status)` | Read/write the flag |
| `satcom_object` | `sat_com_model.Satellite` backing this node |
| `sync_position_from_satcom(altitude_unit="m")` | Copy latitude/longitude/altitude from `satcom_object.spatial_position`, converting the altitude from metres (default) or taking it as kilometres |
| `start_net_logger(path)` | Start `net_logger` on every interface, detached, writing `<path>/<node>_<iface>.log` |
| `default_qos_is_on`, `program_specific_qos_is_on`, `qos_configuration_count` | Legacy QoS bookkeeping used by the TBF helper |

`sync_position_from_satcom(altitude_unit="m")` makes the altitude unit
explicit. The default `"m"` follows the sat_com movement-model contract and
divides the value by 1000 (`781089` → `781`); `altitude_unit="km"` stores the
value unchanged (`0.034` → `0.034`). Any other unit raises `ValueError`. There
is no magnitude heuristic, so a genuine 50 km altitude reported in metres
becomes 50 km rather than 50,000 km. Missing coordinates log a warning and
leave the previous position in place.

## `GroundStation`

```python
from satgonetem.models import GroundStation

gs = GroundStation("Gnd0")
gs.city = "Berlin"
```

Adds `city` (display label, filled from the sat_com label during sync),
`traffic_models` (free-form list for experiments), and `satcom_object`.
`sync_position_from_satcom()` divides the sat_com altitude by 1000
(metres → kilometres); unlike the satellite version it has no unit flag.

Ground-station links are the only links that use link-budget capacity
calculation (see below).

## `Interface`

An `Interface` stores the intra-container interface name, the emulator-level
name, addressing, state, and an optional peer.

```python
import ipaddress

from satgonetem.models import Interface

iface = Interface("Gnd0.3")
iface.set_ipv4("10.0.0.6", 31)
assert iface.network == ipaddress.ip_network("10.0.0.6/31")
assert iface.get_iname() == "eth3"
```

| Attribute | Meaning |
|---|---|
| `name` | Emulator name, `<node>.<peer_id>`; loopbacks become `lo_<node>` in the manager list |
| `ipv4` | Address string (`""` until assigned) |
| `prefix_len` | `/31` for point-to-point links, `/32` for loopbacks |
| `peer` | The interface at the other end of the link; defaults to `None` until wired (not set for loopbacks) |
| `is_active`, `previously_active` | Mirrors `Link.is_active` after each tick |
| `is_monitored` | Set when interface statistics are collected |
| `type` | `"lo"`, `"InterSatelliteLink"`, `"GroundStationLink"`, or `"GroundObjectLink"` |
| `delay` | Link delay in ms (informational copy) |
| `mac_address` | Cached MAC, resolved lazily by `Link.get_mac_addresses()` |

`set_ipv4()` validates the address and prefix. `set_ip()` is a backward-
compatible alias. `network` returns the `IPv4Network` or `None` when no
address is set. Interfaces compare and hash by `name`: equal names yield equal
hashes, and interfaces can be used as set members or dict keys.

**Rename before hashing.** Because the hash is derived from `name`, renaming an
`Interface` after it has been inserted into a `set` or used as a `dict` key
changes its hash and makes the existing entry unreachable. Finish renaming
first, then insert. `InterfaceMgrMixin` follows this order: it renames each
node's loopback to `lo_<node>` and only then appends it to the interface list,
so do not rename loopbacks again after they have been indexed.

`get_iname()` returns `"lo"` for loopbacks and `"eth" + name.split(".")[1]`
otherwise. This is the name tools should use inside the container. A
non-loopback name without the expected `<iface>.<id>` form raises
`ValueError` naming the offending interface (instead of an `IndexError`).

## `Link`

A `Link` joins two `Node` objects and is the unit of delay/rate configuration.
It is constructed by the topology sync layer, not normally by users.

```python
from satgonetem.models import Link

link = Link(
    source=sat,
    target=gs,
    distance=1_200_000.0,        # metres
    type="GroundStationLink",
    default_capacity_kbps=100_000,
)
assert link.delay == 4            # int(distance / c * 1000)
```

### Constructor arguments

| Argument | Default | Meaning |
|---|---|---|
| `source`, `target` | required | Endpoint nodes |
| `distance` | required | Distance in **metres** |
| `type` | required | `InterSatelliteLink`, `GroundStationLink`, or `GroundObjectLink` |
| `direction` | `""` | Optional ISL direction metadata |
| `is_active` | `True` | Whether the link is currently usable |
| `default_capacity_kbps` | `1000` | Fallback capacity per peer |
| `use_budget` | `False` | Compute GSL capacity from the link budget |
| `link_budget_config` | `None` | Frequencies/bandwidth used by the budget |
| `downlink_capacity_strategy` | ModCod | Optional `CapacityStrategy` for downlink |
| `uplink_capacity_strategy` | Shannon | Optional `CapacityStrategy` for uplink |
| `loss` | `0.0` | Packet loss percentage in `[0, 100]` |
| `jitter` | `0` | Delay jitter in milliseconds, non-negative |
| `admin_up` | `True` | Administrative link state ANDed with `is_active` |

### Delay and distance

`delay` is an integer number of milliseconds:

```text
delay_ms = int(distance_m / 299_792_458 * 1000)
```

Because it is truncated, two distances within the same millisecond produce
the same `delay`. `distance` itself remains the precise value and is what
routing weights use.

`update_distance_and_delay(new_distance)` recomputes the delay and sets
`to_update` when the millisecond value changes. It also refreshes budget-based
capacity when the distance of a ground-station link changes.

`sync_distance_from_satcom_and_delay()` recomputes the distance from the two
node positions with `distance_3d_km()` and then delegates to
`update_distance_and_delay()`. It requires `satcom_object` to be set.

### Capacity

`peer1_capacity` and `peer2_capacity` are the per-direction capacities in
kbps. `update_link_capacities()`:

- Uses the link budget only for `GroundStationLink` with `use_budget=True`.
- Otherwise copies `default_capacity_kbps` into both peers.

For budget links, `peer1_capacity` is the satellite → ground-station downlink
(ModCod strategy) and `peer2_capacity` is the ground-station → satellite
uplink (Shannon strategy). Elevation geometry requires `latitude`, `longitude`,
and `altitude` on both nodes; `_compute_elevation_angle()` raises `ValueError`
instead of inventing a fallback angle. A missing antenna, incomplete or
invalid position, or failed budget makes both peers fall back to
`default_capacity_kbps` with a logged warning; no default `(0, 0)` ground
coordinates are substituted.

`get_capacity()` returns `peer1_capacity`. `refresh_link_capacities()` returns
`True` when either value changed and marks `to_update`.

### State flags

The topology sync layer uses four flags consumed by `bulk_link_operations()`:

| Flag | Meaning |
|---|---|
| `to_add` | Create this link in the backend |
| `to_update` | Push new delay/rate/loss/jitter to the backend |
| `to_remove` | Delete the link from the backend |
| `to_delete` | Set while a backend link deletion is unconfirmed; cleared on success so failed deletes stay retryable |

### Impairments and administrative state

`loss` and `jitter` are part of the link's runtime QoS state:

- `loss` is a packet-loss percentage accepted by GoNetEm in `[0, 100]`. It is
  validated as finite and stored as a `float`. Booleans, strings, NaN, and
  infinities are rejected.
- `jitter` is an integer number of milliseconds and must be non-negative.
  Booleans and floats are rejected so the protobuf `int32 jitter` field is
  never silently truncated.

`TopologySyncMixin` seeds new links from the manager's optional
`link_loss_percent` / `link_jitter_ms` attributes (defaulting to `0.0` / `0`),
so a `NetworkConfig` extension can introduce defaults without changing the
link factory. The project builder emits `jitter` on every generated QoS
mapping and emits `loss` only when it is non-zero, keeping the historical
default YAML unchanged.

`admin_up` is independent of the satcom-derived `is_active` flag.
`update_interfaces_state()` sets each peer interface active only when
`is_active and admin_up`. This lets an explicit administrative shutdown
(`set_link_state(link, up=False)`) survive later topology ticks, while
`Link.is_active` remains owned by the topology sync layer.

### Helpers

`get_names()` returns the two emulator interface names
(`"<source>.<target_id>"`, `"<target>.<source_id>"`).
`update_interfaces_state()` copies `is_active and admin_up` onto both peer
interfaces.
`get_mac_addresses()` reads and caches the MAC of both endpoints, raising
`ValueError` if the peer interfaces are missing or do not match.

Two links are equal when their source and target nodes are equal, which is
consistent with the `frozenset` key used by `TopologyManager.links`.

## `RoutingEntry`

One IPv4 route, normally created by a routing daemon and stored in
`Node.ipv4_routing_table`.

```python
from satgonetem.models import Interface, RoutingEntry

iface = Interface("Sat0.1")
entry = RoutingEntry(
    destination="10.128.0.1",
    interface=iface,
    gateway="10.0.0.5",
    prefix=32,
    target_node="Sat0",
    source_node="Gnd0",
)
assert entry.destination == "10.128.0.1"
assert entry.get_prefix() == "/32"
```

The constructor validates the protocol (`ipv4` only), the prefix range
`0..32`, and summarizes `destination` and `source` to the given prefix.
`from_validated()` skips validation for hot paths where inputs are already
canonical. Equality compares destination, interface, gateway, and prefix.

## MPLS and Segment Routing entries

`mpls_entry.py` models the Linux MPLS routes installed by `SRMPLSDaemon`.
All entry types return both a complete `ip ...` command and a line suitable
for `ip -batch`.

| Class | Purpose | Installed command |
|---|---|---|
| `MPLSConfig` | Label range and SR options | — |
| `SRNodeSIDEntry` | Local delivery of the node's own SID | `ip -f mpls route replace <sid> dev lo` |
| `SRForwardEntry` | Transit forwarding for another node's SID | `ip -f mpls route replace <sid> via inet <next_hop> dev <iface>` |
| `SRLabelStackEntry` | Ingress label stack for a destination | `ip route replace <dest>/<prefix> encap mpls <labels> via <next_hop> dev <iface>` |

`MPLSConfig` clamps `label_range_start` to at least 16 and
`label_range_end` to at most 1048575, then raises `ValueError` if the start is
not strictly below the end. Its defaults differ from the Pydantic
`MPLSConfiguration` in `sat_com_models.py`: `use_php=False` here versus
`php=True` there.

`SRLabelStackEntry.label_stack` is ordered outermost first and joined with `/`
for the kernel command: the first label is the top of the stack and is popped
by the first transit node, after which the kernel processes the next label. An
empty stack degrades to a plain IP route. Linux accepts at most
`MAX_NEW_LABELS` (30) pushed labels, so a path with more than 30 transit nodes
cannot be installed as a single stack.

`SegmentRoutingManager.get_label_stack_for_path()` takes the complete path
from source to destination. The source is never labelled, and the destination
is labelled only when PHP is disabled (`use_php=False`). The ingress route and
the stack reported by `set_sr_custom_path()` use this same builder, so the
reported stack matches the ingress stack installed for addressable sources.
The side effects of `set_sr_custom_path()` can lag: routes are rebuilt only
while the topology is active, and installation errors are logged rather than
raised, so a failed install can leave the reported stack ahead of the kernel.

## Declarative configuration models

`sat_com_models.py` provides Pydantic v2 models that mirror the subset of the
sat_com configuration used by tests and serialization:

- `ConstellationProperty`, `OrbitalConnectivityProperty`, `WalkerShell`
- `ConnectivityProperties`, `GroundObject`
- `CustomSatellite`
- `MPLSConfiguration`, `NetworkProperties`, `Configuration`

These are **not** the classes `TopologyManager.from_satcom()` accepts. That
method expects `sat_com_builder.models.SimulationProperty`. The Pydantic
models here are useful for validating dictionaries and for documenting the
on-disk schema; they are not imported by the service layer.

## QoS mixin

`QoSCapableMixin.initialise_global_qos()` is a legacy traffic-experiment
helper that retunes an existing TBF qdisc (`parent 1:1 handle 10:`) so a
burst of flows does not overflow the modelled link buffer.

```python
sat.initialise_global_qos(
    interface_name="eth3",
    num_of_flows=4,
    demand_bit_rate=100,       # Mbps per flow
    isl_capacity=1000,         # Mbps
    num_of_bytes=10_000_000,
    tbf_config="burstlimit",   # or "limit"
    link=link,
)
```

It reads the current qdisc with `tc qdisc show`, parses every output line (a
single-line qdisc display is valid), derives the required burst/limit from the
demand, and issues `tc qdisc change`. The qdisc must already exist; the method
logs an error and returns if no TBF line is found. A non-positive
`demand_bit_rate` or `isl_capacity` raises `ValueError`. When the offered rate
fits within capacity, or the derived limit/burst is non-positive, the method
logs that no shaping is needed and leaves the qdisc unchanged.

## Putting it together

```python
from satgonetem.models import GroundStation, Link, Satellite

sat = Satellite("Sat0")
gs = GroundStation("Gnd0")
sat.position.update(latitude=48.85, longitude=2.35, altitude=550.0)
gs.position.update(latitude=48.85, longitude=2.35, altitude=0.035)

link = Link(sat, gs, distance=550_000.0, type="GroundStationLink")
sat.create_interface("Sat0.0").set_ipv4("10.0.0.5", 31)
gs.create_interface("Gnd0.0").set_ipv4("10.0.0.4", 31)

print(link.delay, link.get_capacity())
```

In normal use these objects are created and kept in sync by
`TopologyManager`; see [TopologyManager internals](services.md) and
[IPv4 Addressing](addressing.md) for the surrounding lifecycle.

## Related documentation

- [TopologyManager public API](topology_manager_api.md)
- [TopologyManager internals](services.md)
- [IPv4 Addressing](addressing.md)
- [Routing](routing.md)
- [Link Budget](link_budget.md)
