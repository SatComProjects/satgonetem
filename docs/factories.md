# Emulator metadata factories

Phase 5 adds an optional, thin metadata layer on top of satComTopology V5
objects. It does **not** replace SatGoNetEm's `Node`/`Link` classes.

## What is added

`satgonetem/models/satcom_extensions.py` defines pydantic subclasses of V5's
topology objects:

| Class | Extra fields |
|---|---|
| `EmulatorSatellite` | `emulator_id`, `container_name`, `address_block` |
| `EmulatorGroundStation` | same |
| `EmulatorUserTerminal` | same |
| `EmulatorPointOfPresence` | same |

Factories build these classes:

```python
from satgonetem.models.satcom_extensions import (
    emulator_event_factories,
    emulator_topology_object_factories,
)
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_satcom(
    project,
    topology_object_factories=emulator_topology_object_factories(),
    event_factories=emulator_event_factories(),
)
```

After the initial sync and address-plan build, the manager fills:

- `emulator_id` — stable integer node ID;
- `container_name` — `Sat*`, `Gnd*`, `Usr*`, or `Pop*`;
- `address_block` — the node's planned IPv4 block, as a string.

The V5 event factories add container names to ISL/GSL/UTL events. The event
sync uses those names when present instead of rebuilding `Sat{id}` strings.
Custom fields are opt-in: default V5 events and default node classes are
unchanged.

## Why not full model unification

The emulator still keeps its own wrappers because `Node` owns:

- the Docker container handle and namespace operations;
- interfaces, MAC addresses, and the planned `/31` addresses;
- routing tables and daemon state;
- QoS and traffic state.

V5 objects are pydantic models and are replaced or validated on their own
lifecycle; making `Node` a pydantic V5 model would mix those concerns and is a
large, risky refactor. The metadata layer provides the useful part — stable
identity and event payloads — without that coupling.

The per-link `/31` allocator also stays in SatGoNetEm: V5 has no link-address
concept.

## Limitations

- The factories are opt-in; default `from_satcom()` uses V5's default classes.
- Initial events are not replayed. Metadata is filled by the initial sync.
- Full `Node`/`Link` unification remains a separate design decision, not part
  of Phase 5.
