# Hardware-in-the-Loop Manager

## Purpose

`HILManager` works **alongside** `GoNetEmLauncher`. The launcher continues to manage all satellites, ISLs, and
non-HIL ground stations as normal. HILManager takes over only for the ground
stations declared in its map, replacing their emulated containers with a
transparent L2 bridge to physical hardware.

## Architecture

```
TopologyManager
  |
  +-- NetworkLauncher (GoNetEmLauncher)
  |     manages: SAT containers, ISLs, non-HIL GSLs
  |
  +-- HILManager (optional, set before start_gonetem)
        manages: HIL GSL veth creation, host bridges, SAT-side QoS
```

For each HIL ground station link:

```
  SAT container ns          host ns              physical HW
  +------------+           +-----------+        +-----------+
  | eth{gnd_id}|<---veth-->| hil{id}   |--+    |           |
  | (netem+TBF)|           | (bridge   |  +--> | {hw_iface}|
  +------------+           |  slave)   |       | (bridge   |
                           +-----------+       |  slave)   |
                                               +-----------+
                           brhil{gnd_id} (Linux bridge)
```

- The satellite container's `eth{gnd_id}` interface carries netem+TBF qdiscs
  emulating orbital propagation delay and capacity.
- `hil{gnd_id}` is the host-namespace veth peer, enslaved to a Linux bridge.
- The physical hardware interface is also enslaved to the same bridge,
  forming a transparent L2 path from satellite container to hardware.
- No IP address is assigned to the bridge or host-side veth. The hardware
  device is responsible for its own L3 addressing.

## Privileges

All kernel operations require elevated privileges:

| Operation | Capability |
|---|---|
| `setns()` into container namespaces | `CAP_SYS_ADMIN` |
| Creating veth pairs (pyroute2) | `CAP_NET_ADMIN` |
| Creating and managing Linux bridges | `CAP_NET_ADMIN` |
| `nsenter` for `tc` commands | `CAP_SYS_ADMIN` |

Run the emulator as root or grant these capabilities explicitly. The
underlying GoNetEmLauncher already requires root for
container management; HILManager does not change that requirement.

## Usage

```python
from satgonetem.launchers import HILManager

tm = TopologyManager.from_satcom(...)

tm.hil_manager = HILManager(
    gnd_hardware_map={
        "Gnd0": "eth1",    # Gnd0 bridged to eth1
        "Gnd1": "enp3s0",  # Gnd1 bridged to enp3s0
    },
    gnd_capacity_kbps=50_000,
)

tm.start_gonetem()
```

TopologyManager routes all HIL ground station operations through `hil_manager`
automatically. The launcher (GoNetEmLauncher) is unaware of
HIL and handles only satellites and non-HIL ground stations.

## PID Resolution

HILManager reads `node.container_pid` from the satellite node object. This
attribute is set by any launcher during `start_containers`. HILManager holds
no reference to the launcher and works with GoNetEmLauncher as long as `container_pid` is populated on satellite nodes.

## Handovers

When an orbital update causes a ground station to switch its serving satellite:

1. `TopologyManager._execute_link_delete(old_link)` routes to
   `hil_manager.teardown_link(old_link)`:
   - Bridge `brhil{gnd_id}` is deleted. The kernel releases the hardware
     interface and host-side veth automatically.
   - The SAT-side veth `eth{gnd_id}` is deleted via `nsenter`. The kernel
     removes the host-side peer `hil{gnd_id}`.

2. `TopologyManager._execute_link_add(new_link)` routes to
   `hil_manager.setup_link(new_link)`:
   - A new veth pair is created with the new satellite container as the
     SAT end and the host namespace as the GND end.
   - A new bridge `brhil{gnd_id}` is created and the hardware interface is
     re-enslaved.
   - netem+TBF qdiscs are applied to the new SAT-side interface.

The hardware interface is momentarily unmastered between steps 1 and 2,
corresponding to the natural handover break period.

## QoS Model

Only the satellite-side interface carries emulated qdiscs:

- `netem delay {ms}ms` - propagation delay computed from orbital distance
  at the speed of light.
- `tbf rate {mbit}mbit` - capacity from `peer1_capacity`, `capacity`, or
  the configured `gnd_capacity_kbps` default, in that priority order.

The hardware side is unmodified. Physical path characteristics (RF delay,
modem buffering) are additive to the emulated delay.

## Interface Naming

| Interface | Location | Example |
|---|---|---|
| `eth{gnd_id}` | SAT container namespace | `eth0` (Sat to Gnd0) |
| `hil{gnd_id}` | host namespace | `hil0` |
| `brhil{gnd_id}` | host namespace | `brhil0` |
| `{hw_iface}` | host namespace | `eth1` (from gnd_hardware_map) |

## Teardown

`stop_gonetem()` and `force_stop_gonetem()` both call `hil_manager.teardown_all()`
before `launcher.close_project()`:

1. All Linux bridges deleted - hardware interfaces released.
2. Launcher removes SAT containers - kernel deletes veth pairs, host-side
   peers removed automatically.

## Running Tests

Tests mock all kernel and subprocess calls; no root or hardware needed:

```bash
pytest tests/launchers/test_hil_manager.py -v
```
