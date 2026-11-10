# IPv4 Address Plan API

`satgonetem.addressing.ipv4` is a standalone, dependency-free allocator for
the data-plane IPv4 plan. It knows nothing about Docker, gRPC, or topology
models: callers register nodes and opaque link keys, and receive immutable
address objects.

The conceptual overview (pools, ownership, and the runtime sequence) is in
[IPv4 Addressing](addressing.md). This page is the class-level reference.

```python
from satgonetem.addressing import Ipv4AddressPlan, NodeKind

plan = Ipv4AddressPlan()
gs = plan.register_node(1, NodeKind.GROUND_STATION)
sat = plan.register_node(0, NodeKind.SATELLITE)
link = plan.allocate_link(frozenset({"Gnd1", "Sat0"}), 1, 0)

assert str(gs.loopback_ip) == "10.0.0.1"
assert str(sat.loopback_ip) == "10.128.0.1"
assert link.prefix_len == 31
```

## Exports

| Name | Kind | Meaning |
|---|---|---|
| `Ipv4AddressPlan` | class | Thread-safe allocator for node blocks and `/31` links |
| `NodeKind` | `str` enum | `GroundStation`, `Satellite`, `UserTerminal`; `from_type_name()` maps the model's `Node.type` string |
| `NodeAddress` | frozen dataclass | Block and loopback of one node |
| `LinkAddress` | frozen dataclass | Addresses of one point-to-point link |
| `AddressPlanError` | `ValueError` subclass | Base error |
| `AddressPlanConfigurationError` | `AddressPlanError` | Invalid pools or prefix lengths |
| `AddressPlanExhausted` | `AddressPlanError` | No block or link slot left |

## Configuration and validation

```python
plan = Ipv4AddressPlan(
    ground_station_pool="10.0.0.0/10",
    satellite_pool="10.128.0.0/9",
    user_terminal_pool="10.64.0.0/10",
    ground_station_prefix_len=20,
    satellite_prefix_len=27,
    user_terminal_prefix_len=20,
)
```

Pools may be `str` CIDR or `ipaddress.IPv4Network`. Validation happens in the
constructor:

| Check | Failure |
|---|---|
| CIDR parses with no host bits | `AddressPlanConfigurationError` |
| Prefix length is an integer in `[0, 32]` | `AddressPlanConfigurationError` |
| Block prefix is longer than the pool prefix | `AddressPlanConfigurationError` |
| Block is large enough for a loopback plus at least one `/31` | `AddressPlanConfigurationError` |
| Pool is not multicast, loopback, link-local, or starting at `0.0.0.0` | `AddressPlanConfigurationError` |
| Pool is not globally routable | `AddressPlanConfigurationError` |
| The three pools do not overlap | `AddressPlanConfigurationError` |

The "not global" rule means `8.8.8.0/24` is rejected while RFC 1918, shared
(`100.64.0.0/10`), and other non-global ranges are accepted.

## Node registration

```python
gs = plan.register_node(node_id=1, kind=NodeKind.GROUND_STATION)
```

`register_node()` assigns the next free block **for that kind**, in
registration order, and returns a `NodeAddress`:

| Field | Meaning |
|---|---|
| `node_id` | Caller-supplied numeric ID |
| `kind` | `NodeKind` |
| `slot` | Zero-based block index within the kind's pool |
| `block` | `ipaddress.IPv4Network` for this node |
| `loopback` | `ipaddress.IPv4Interface` with `/32` |
| `loopback_ip` | Property returning the loopback address as a string |

Registration is idempotent for the same `(node_id, kind)`: it returns the
existing `NodeAddress`. Re-registering the same ID with a different kind
raises `AddressPlanError`. Node IDs must be non-negative integers; `bool` is
rejected even though it is an `int` subclass.

Because blocks are assigned in registration order, reproducibility is the
caller's responsibility. `TopologyManager` registers satellites and ground
stations sorted by numeric ID.

Every node block has this layout:

```text
block base + 0       network address (never assigned)
block base + 1       loopback /32
block base + 2..3    reserved
block base + 4       first /31 pair (slot 0)
block base + 6       second /31 pair (slot 1)
...
```

The number of `/31` slots in a block is `(block_size - 4) // 2`. For the
default satellite `/27` block that is 14 slots; for a `/20` ground-station
block it is 2046.

## Link allocation

```python
key = frozenset({"Gnd1", "Sat0"})       # any hashable key works
address = plan.allocate_link(key, source_id=1, target_id=0)
```

`allocate_link()` is idempotent per `link_key`: the first call allocates a
slot, later calls return the same `LinkAddress`. A self-link raises
`AddressPlanError`.

### Ownership

The owner is chosen with `NodeKind.priority`, then by lower node ID:

| Priority | Kind |
|---|---|
| 0 | `GROUND_STATION` |
| 1 | `USER_TERMINAL` |
| 2 | `SATELLITE` |

The owner's block supplies the `/31`. The winning endpoint receives the lower
address of the pair, so the result does not depend on the incidental
`source_id`/`target_id` orientation.

`LinkAddress` exposes:

| Field | Meaning |
|---|---|
| `owner_id` | Node ID whose block owns the slot |
| `slot` | Zero-based `/31` slot within the owner's block |
| `prefix_len` | Always `31` |
| `source` | `IPv4Interface` for `source_id` |
| `target` | `IPv4Interface` for `target_id` |

`release_link(key)` returns the slot to the owner's free list. The allocator
reuses the lowest released slot first, so churn does not permanently consume
the block. Releasing an unknown key is a no-op.

## Introspection

| Method | Returns |
|---|---|
| `node_address(node_id)` | `NodeAddress`; raises `AddressPlanError` if unknown |
| `node_addresses()` | All `NodeAddress` objects in registration order |
| `link_address(key)` | `LinkAddress` or `None` |
| `link_addresses()` | All `LinkAddress` objects in allocation order |
| `pool_for(kind)` | Configured `IPv4Network` |
| `prefix_len_for(kind)` | Configured block prefix length |
| `capacity_for(kind)` | Number of node blocks available in the pool |
| `link_capacity_for(node_id)` | Number of `/31` slots in that node's block |

`capacity_for()` counts **node blocks** in the pool (for example 262,144
satellite `/27` blocks in `10.128.0.0/9`), while `link_capacity_for()` counts
**link slots per node block** (14 for a `/27`).

## Errors and exhaustion

```python
from satgonetem.addressing import AddressPlanExhausted

small = Ipv4AddressPlan(
    satellite_pool="10.128.0.0/24",
    satellite_prefix_len=28,
)
small.register_node(0, NodeKind.SATELLITE)
small.register_node(1, NodeKind.SATELLITE)
for i in range(6):
    small.allocate_link(f"link-{i}", 0, 1)

try:
    small.allocate_link("overflow", 0, 1)
except AddressPlanExhausted as exc:
    print("block full:", exc)
```

- `register_node()` raises `AddressPlanExhausted` when the kind's pool has no
  free block left.
- `allocate_link()` raises `AddressPlanExhausted` when the owner's block has
  no free `/31` slot.
- Unknown node IDs, self-links, and kind mismatches raise `AddressPlanError`.
- Configuration problems raise `AddressPlanConfigurationError`.

All three are subclasses of `ValueError`, so existing `except ValueError`
handlers also catch them.

## Thread safety

Every public mutating and reading method takes an internal `RLock`. It is
safe to register nodes and allocate/release links from multiple threads, but
registration order then becomes nondeterministic. For reproducible plans,
register from one thread in a documented order.

## Integration with TopologyManager

`InterfaceMgrMixin._initialize_address_plan()` builds the plan, registers
satellites and ground stations in sorted-ID order, and allocates one link per
`Link`. `_build_interfaces_from_link()` then copies the returned `/31` onto
the two `Interface` objects. `_cleanup_removed_links_sequential()` calls
`release_link()` when a link disappears so later dynamic links can reuse the
slot.

The runtime sequence is:

```python
topology = TopologyManager.from_file("topology_files/iridium_topology.json")
# topology.address_plan exists now; no container commands have run

topology.start_gonetem()
topology.set_ip_addresses()   # pushes the planned /31s and loopbacks
```

See [TopologyManager Internals](services.md) for the full initialization and
tick pipeline.

## Related documentation

- [IPv4 Addressing](addressing.md) — pools, block layout, and ownership rules
- [TopologyManager Internals](services.md) — plan construction and address push
- [Data Model Layer](models.md) — `Interface`, `Node.address_block`, `Link`
- [Controller Plane](controller.md) — separate out-of-band address scheme
