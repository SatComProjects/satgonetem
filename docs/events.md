# Event-driven topology sync (experimental)

SatGoNetEm can consume satComTopology V5's `EventBus` instead of rediscovering
link changes from a full scan. The path is implemented and tested, but **off by
default**: on V5.0.3 it is slower than the polling path at the scales tested.
This page records how it works, how to enable it, and why polling remains the
default.

## Enable it

```python
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

topology = TopologyManager.from_satcom(
    project,
    NetworkConfig(
        topology_events=True,
        event_reconcile_interval=30,  # full scan every 30 ticks; 0 disables
    ),
)
```

`topology_events=False` preserves the Phase 1 polling path.

## What happens on a tick

```
time_manager.tick()
    └─ V5 publishes ISL_*/GSL_*/UTL_* events
         └─ EventCollector (async handlers, append-only, no emulator mutations)
              └─ update_simulation drains the queue
                   └─ LinkDelta coalesces events per link
                        ├─ new link      -> create Link, mark to_add
                        ├─ removed link  -> mark to_remove
                        └─ re-created    -> refresh existing Link
                   └─ refresh every existing Link from its V5 object
                        (distance, active state, duration, elevation, optical flags)
                   └─ delete -> add -> update -> routing once -> interfaces
```

The collector only appends events. The simulation thread drains the queue and
applies one coalesced delta under the existing `_simulation_lock`, so event
handlers never touch Docker, gRPC, routing, or emulator collections.

Only `ISL_*`, `GSL_*`, and `UTL_*` created/removed events are subscribed.
`ISL_UPDATED`/`GSL_UPDATED` events are deliberately not subscribed: continuous
state is refreshed directly from the V5 link objects. UTL events are collected
and ignored until user terminals become first-class emulator nodes.

## Reconciliation

Events are deltas, not snapshots. Some V5 strategies keep a link and update it
in place without publishing an update event. Every
`event_reconcile_interval` ticks the manager runs the polling scan/diff once and
merges the result with the event delta. This repairs missed events and handles
links that V5 recreated without a publish. Set the interval to `0` to disable
reconciliation at your own risk.

## Async delivery

V5 publishes synchronously when `tick()` runs without an event loop. If the tick
runs inside a running asyncio loop, V5 schedules handlers as tasks and the
collector may still be empty when the tick returns. Use the async wrapper:

```python
await topology.next_step_async()
```

It runs the blocking tick in a worker thread, where V5 delivers events
synchronously. Do not call the sync `next_step()`/`update_simulation()` from a
running event loop in event mode.

## Benchmark and decision

Measured with `scripts/benchmark_topology_stages.py --event-mode`, same method
as the Phase 0 baseline (100 GS, 10 measured ticks after warm-up, one process
per scenario):

| Case | Polling tick p50 | Event tick p50 | Polling `link_sync` | Event `link_sync` |
|---|---:|---:|---:|---:|
| 720 basic, 5 s | 0.023 s | 0.069 s | 0.003 s | 0.007 s |
| 3,360 basic, 60 s | 0.161 s | 0.393 s | 0.010 s | 0.070 s |
| 3,360 advanced, 60 s | 0.702 s | 0.930 s | 0.013 s | 0.072 s |

The regression is in V5's event delivery itself: once an EventBus is attached,
V5 constructs and publishes every update event even when the emulator has no
subscriber for that event type. At 3,360 satellites that is O(E) pydantic events
plus one `asyncio.run()` per event per tick. A dummy no-op bus still added about
0.04 s to a 720-satellite tick, so this is not just SatGoNetEm's handler work.

Decision: keep the event path behind `topology_events=False` and continue using
polling by default. Revisit when satComTopology can skip event construction when
a type has no subscribers, or can batch delivery. The event path is otherwise
useful for small topologies and as the foundation for Phase 3 link-lifetime
work.
