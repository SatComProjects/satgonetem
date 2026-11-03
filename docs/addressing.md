# IPv4 Addressing

SatGoNetEm allocates IPv4 addresses with a deterministic topology-wide plan.
Addresses are generated from node blocks, not from a hash of node and peer IDs.
The default plan lives entirely inside the private `10.0.0.0/8` aggregate and
uses point-to-point `/31` links and `/32` loopbacks.

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

## Routing

Static routing installs `/32` routes to destination loopbacks. OSPF and IS-IS
use the prefix stored on each `Interface` (`/31` for links, `/32` for
loopbacks). No routing code assumes a global `/15` summary.

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

User terminals are first-class emulator nodes named `Usr{topology_uniq_id}`.
They use the user-terminal pool, receive a `/32` loopback and `/31` link
addresses, and are stored in `TopologyManager.ground_stations` alongside
gateways so the launcher, routing, and graph paths are shared. A V5
`UserTerminalLink` is modelled as an emulator `UserTerminalLink`.

## Controller plane

The optional controller plane is an out-of-band management network and is
independent of the data-plane pools above. It retains its own `248.0.0.0/16`
mapping and 13-bit node field in `ControllerMixin.build_controller_ip()`; the
data-plane satellite capacity is not limited by it.
