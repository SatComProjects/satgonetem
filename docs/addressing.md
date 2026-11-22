# IPv4 and IPv6 Addressing

SatGoNetEm allocates IPv4 addresses with a deterministic topology-wide plan.
Addresses are generated from node blocks, not from a hash of node and peer IDs.
The default plan lives entirely inside the private `10.0.0.0/8` aggregate and
uses point-to-point `/31` links and `/32` loopbacks.

An optional IPv6 plan can run alongside IPv4. IPv4 is always enabled and keeps
its allocation order; IPv6 is opt-in through `NetworkConfig.ipv6_enabled` and
only covers addressing (routing and traffic remain IPv4-only).

## Default pools

| Purpose | Pool | Block per node | Capacity |
|---|---|---|---|
| Ground stations | `10.0.0.0/10` | `/20` | 1,024 ground stations |
| User terminals | `10.64.0.0/10` | `/20` | 1,024 user terminals |
| Satellites | `10.128.0.0/9` | `/27` | 262,144 satellites |

The pool and block sizes are configured through `NetworkConfig`:

```python
from satgonetem.services.topology_satcom import NetworkConfig

config = NetworkConfig(
    ground_station_pool="10.0.0.0/10",
    satellite_pool="10.128.0.0/9",
    user_terminal_pool="10.64.0.0/10",
    ground_station_prefix_len=20,
    satellite_prefix_len=27,
    user_terminal_prefix_len=20,
)
```

The pools must not overlap, must be valid IPv4 networks with no host bits set,
must be large enough for the configured block prefix, and must be non-global
(private, shared, or reserved) ranges. A satellite `/28` raises the capacity
to 524,288 satellites, at the cost of fewer `/31` slots per satellite.

## Node block layout

Every node owns one block:

```
<block base + 0>        network address (never assigned)
<block base + 1>/32     loopback
<block base + 2..3>     reserved
<block base + 4>/31     first point-to-point link
<block base + 6>/31     second point-to-point link
...
```

The loopback is the only address used as a routing destination. Traffic tools,
OSPF/IS-IS advertisement, and static routes target it directly; interface
addresses are for link-local forwarding only.

Example from `topology_files/test_topology.json`:

| Node | Block | Loopback | First link |
|---|---|---|---|
| `Gnd66` | `10.0.0.0/20` | `10.0.0.1/32` | `10.0.0.4/31` |
| `Sat0` | `10.128.0.0/27` | `10.128.0.1/32` | `10.128.0.4/31` |

## Link ownership

Each link receives one `/31`. The owner determines the block from which the
`/31` is taken:

1. A ground-station endpoint owns the link.
2. Otherwise a user-terminal endpoint owns it.
3. Otherwise the lower satellite ID owns it.

The owner always receives the lower address of the `/31`. This makes the
address pair independent of the incidental `Link.source` / `Link.target`
orientation. A ground-station link therefore has both addresses inside the
ground station's `/20`, including the satellite-side interface.

## Optional IPv6 addressing

IPv6 uses the same node kinds, block layout, and link-ownership rules as IPv4,
with its own private ULA pools:

| Purpose | Pool | Block per node | Capacity |
|---|---|---|---|
| Ground stations | `fd00:1::/40` | `/64` | 16,777,216 ground stations |
| Satellites | `fd00:2::/40` | `/64` | 16,777,216 satellites |
| User terminals | `fd00:3::/40` | `/64` | 16,777,216 user terminals |

Enable it before the topology is built, either directly or with a `from_file`
override:

```python
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

config = NetworkConfig(
    ipv6_enabled=True,
    ipv6_ground_station_pool="fd00:1::/40",
    ipv6_satellite_pool="fd00:2::/40",
    ipv6_user_terminal_pool="fd00:3::/40",
    ipv6_ground_station_prefix_len=64,
    ipv6_satellite_prefix_len=64,
    ipv6_user_terminal_prefix_len=64,
)
topology = TopologyManager.from_file("topology_files/test_topology.json", config)
```

Every node block has the same layout as IPv4, using IPv6 prefix sizes:

```
<block base + 0>          network address (never assigned)
<block base + 1>/128      loopback
<block base + 2..3>       reserved
<block base + 4>/127      first point-to-point link
<block base + 6>/127      second point-to-point link
...
```

`Interface.ipv6` and `Interface.ipv6_prefix_len` are independent of
`Interface.ipv4` and `Interface.prefix_len`; `Interface.network6` returns the
IPv6 network. Link ownership is identical to IPv4: ground station first, then
user terminal, then the lower satellite ID, and the owner receives the lower
address of the `/127`.

The IPv6 pools must be IPv6 networks with no host bits set, must be private or
unique-local (not global, multicast, loopback, link-local, or unspecified), must
not overlap each other, and must be larger than their block prefix. A block must
also be large enough for a `/128` loopback plus at least one `/127` link.
`NetworkConfig.__post_init__` constructs the planner when IPv6 is enabled, so a
bad pool fails before any topology node is created. With IPv6 disabled,
existing IPv4 allocations and generated project output are unchanged.

### Applying IPv6 at runtime

```python
topology.start_gonetem()
topology.set_ip_addresses()          # pushes IPv4 and IPv6 when enabled
topology.set_ipv6s_for_all_nodes()   # IPv6 only
```

`set_ipv6s_for_all_nodes()` issues one batched
`ip -6 -force -batch` command per node in parallel using `ip addr replace`, so
repeated calls are idempotent. Link interface prefixes must be `/127` or `/128`;
anything else raises `ValueError`. `set_ip_addresses()` attempts both families
even when one fails and raises after both attempts.

## Routing

Static routing installs `/32` routes to destination loopbacks. OSPF and IS-IS
use the prefix stored on each `Interface` (`/31` for links, `/32` for
loopbacks). No routing code assumes a global `/15` summary. IPv6 routing
protocols and IPv6 traffic tools are not implemented.

## Runtime sequence

Address objects exist as soon as the topology is initialized:

```python
topology = TopologyManager.from_file("topology_files/iridium_topology.json")
# topology.address_plan is built during __init__/init

topology.start_gonetem()       # containers and veths
topology.set_ip_addresses()    # push addresses into the containers
```

`set_ip_addresses()` is idempotent: it uses `ip addr replace`, batches all
addresses for a node into one command, and runs nodes in parallel. It raises
after all nodes have been attempted if any node fails.

## User terminals

The plan supports user-terminal blocks and tests them, but the current
emulator does not create user-terminal nodes or user-terminal links. UT links
are skipped by topology sync. Full UT emulation is a separate feature.

## Controller plane

The optional controller plane is an out-of-band management network and is
independent of the data-plane pools above. It retains its own `248.0.0.0/16`
mapping and 13-bit node field in `ControllerMixin.build_controller_ip()`; the
data-plane satellite capacity is not limited by it.

## Related documentation

- [Data Model Layer](models.md) — `Interface`, `Node.address_block`, and link models
- [TopologyManager Internals](services.md) — when the address plan is built and applied
- [Controller Plane](controller.md) — the separate out-of-band addressing scheme
