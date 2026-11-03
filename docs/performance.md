# Topology update performance

This page records the Phase 0 baseline for `TopologyManager.update_simulation()`
and defines the tick budget used by the satComTopology V5 adoption work
(tracking issue #139).

The baseline measures the **polling path**: every tick scans all links,
rebuilds link keys, refreshes distances, diffs add/update/remove sets, and
updates the emulator's link objects. Later phases will compare the event-driven
path against these numbers.

This snapshot predates the Phase 1 graph and distance cleanup. Phase 1 removes
node positions/model data from the routing graph and reads V5's precomputed
`distance_km`; re-run the benchmark after Phase 1 to measure that delta.

## Scope and method

- satComTopology `5.0.3`, Python `3.14.8`, one process per scenario.
- One warm-up tick, then 10 measured ticks.
- 100 ground stations, each allowed at most one GSL (the emulator's default
  test-project shape), so the baseline has 100 GSLs. V5's published benchmark
  uses `everything-visible` and produces roughly 482 GSLs; the two tables are
  not directly comparable.
- No Docker, GoNetEm, traffic, or routing. `link_add`, `link_update`,
  `link_delete`, and `routing_apply` are therefore zero in this table; the
  stage timers are wired so a live run reports them.
- `graph p50` is measured separately with `get_current_graph()` after the
  ticks (three repeats), using the current routing-graph path.
- Peak memory is `ru_maxrss` for the process, so each scenario must run in its
  own process to be comparable.

Run a scenario with:

```bash
.venv/bin/python scripts/benchmark_topology_stages.py --list
.venv/bin/python scripts/benchmark_topology_stages.py \
    --scenario starlink-g7-basic-60 --ticks 10
```

The script prints a Markdown row and, with `--json`, a machine-readable result
including the per-stage p50/p95 values.

## Tick budget

The topology engine should consume **at most 50% of the tick interval** on the
target host. The remaining budget belongs to GoNetEm/gRPC, netem updates,
routing, and traffic.

| Tick interval | Topology budget |
|---:|---:|
| 1 s | 0.50 s |
| 5 s | 2.50 s |
| 60 s | 30.0 s |

This is a policy target, not a measured end-to-end limit. At 1 s, Starlink G7
with the advanced optical model already spends about 0.62 s in the topology
engine, so it exceeds the budget before any emulator operation. Use the basic
model, a larger tick interval, or a smaller constellation for that regime.

## Baseline: 60 s ticks

| Scenario | Sats | ISLs | GSLs | Model | Load s | Tick p50 | Tick p95 | Graph p50 | Peak MB |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| starlink-g2-basic-60 | 720 | 1,440 | 100 | basic | 0.11 | 0.043 | 0.053 | 0.005 | 283 |
| starlink-g2-advanced-60 | 720 | 1,440 | 100 | advanced | 0.69 | 0.186 | 0.242 | 0.005 | 310 |
| starlink-1008-basic-60 | 1,008 | 1,512 | 100 | basic | 0.13 | 0.047 | 0.053 | 0.006 | 286 |
| starlink-1008-advanced-60 | 1,008 | 1,512 | 100 | advanced | 0.70 | 0.207 | 0.249 | 0.006 | 314 |
| starlink-g7-basic-60 | 3,360 | 5,040 | 100 | basic | 0.48 | 0.159 | 0.257 | 0.019 | 339 |
| starlink-g7-advanced-60 | 3,360 | 5,040 | 100 | advanced | 2.52 | 0.690 | 0.873 | 0.019 | 432 |

## Baseline: 5 s ticks

| Scenario | Sats | ISLs | GSLs | Model | Load s | Tick p50 | Tick p95 | Graph p50 | Peak MB |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| starlink-g2-basic-5 | 720 | 1,440 | 100 | basic | 0.11 | 0.023 | 0.023 | 0.005 | 282 |
| starlink-g2-advanced-5 | 720 | 1,440 | 100 | advanced | 0.72 | 0.178 | 0.234 | 0.005 | 310 |
| starlink-1008-basic-5 | 1,008 | 1,512 | 100 | basic | 0.13 | 0.027 | 0.029 | 0.006 | 286 |
| starlink-1008-advanced-5 | 1,008 | 1,476 | 100 | advanced | 0.69 | 0.181 | 0.233 | 0.006 | 313 |
| starlink-g7-basic-5 | 3,360 | 5,040 | 100 | basic | 0.46 | 0.085 | 0.183 | 0.018 | 339 |
| starlink-g7-advanced-5 | 3,360 | 5,040 | 100 | advanced | 2.51 | 0.619 | 0.799 | 0.019 | 431 |

The 60 s runs do more work in `link_local_ops` because more links change their
distance and are marked for update. The 5 s runs move less per tick and mark
fewer links dirty. This is a property of the current polling path, not a
property of the tick interval itself.

## Stage breakdown

p50 seconds for three representative cases:

| Stage | 720 basic, 5 s | 3,360 basic, 60 s | 3,360 advanced, 60 s |
|---|---:|---:|---:|
| `tick` (satComTopology update) | 0.0189 | 0.0941 | 0.6209 |
| `position_sync` | 0.0004 | 0.0025 | 0.0041 |
| `link_sync` (scan + distance + diff) | 0.0029 | 0.0104 | 0.0134 |
| `link_local_ops` | 0.0004 | 0.0470 | 0.0475 |
| `link_add` | 0.0000 | 0.0000 | 0.0000 |
| `link_update` | 0.0000 | 0.0000 | 0.0000 |
| `link_delete` | 0.0000 | 0.0000 | 0.0000 |
| `routing_apply` | 0.0000 | 0.0000 | 0.0000 |
| `interface_sync` | 0.0001 | 0.0003 | 0.0003 |
| `event_collection` | 0.0000 | 0.0000 | 0.0000 |
| `total` | 0.0227 | 0.1594 | 0.6897 |

What this says:

- At 3,360 satellites, V5's own `tick` is about 0.09 s in the basic model and
  0.62 s in the advanced model. The advanced cost is in satComTopology, not in
  the emulator's link scan; the event bus does not remove it.
- The emulator's `link_sync` is small in absolute terms (about 0.01 s at 3,360),
  but `link_local_ops` is about 0.05 s at 60 s ticks. The polling path's
  distance-driven update set is therefore a real target for Phase 2.
- `get_current_graph()` is about 0.019 s at 3,360 offline. Live route build and
  installation are separate costs and must be measured with GoNetEm running.
- `event_collection` was a placeholder in Phase 0; Phase 2 now records the
  EventBus drain there when `topology_events=True`.

After Phase 1, the routing graph no longer exports node positions or model data,
and link distances come from V5's `distance_km`. The graph build p50 drops from
about 0.019 s to 0.016 s at 3,360 satellites (about 17%), while the total tick
p50 remains around 0.16 s because `link_sync` and `link_local_ops` still
dominate. Phase 2 must reduce the update work, not only the graph build.

## Phase 2 event path

The event path is implemented behind `topology_events=True`. It is slower on
V5.0.3 because attaching an EventBus makes V5 construct and publish every
update event, even when the emulator has no subscriber for that event type.
See [`docs/events.md`](events.md) for the architecture and the decision to keep
polling as the default. Representative results:

| Case | Polling tick p50 | Event tick p50 | Polling link_sync | Event link_sync |
|---|---:|---:|---:|---:|
| 720 basic, 5 s | 0.023 s | 0.069 s | 0.003 s | 0.007 s |
| 3,360 basic, 60 s | 0.161 s | 0.393 s | 0.010 s | 0.070 s |
| 3,360 advanced, 60 s | 0.702 s | 0.930 s | 0.013 s | 0.072 s |

Both the V5 update and the emulator refresh are more expensive in event mode;
this is not a handler-side problem alone. The Phase 2 performance acceptance
is therefore not met on V5.0.3.

## Reading stage timings

Every `update_simulation()` tick records its stages and exposes them through:

```python
manager.next_step()
print(manager.get_last_stage_timings())
```

The values are seconds. `routing_graph` is nested inside `routing_apply` when
routing updates run, so the stages are not mutually exclusive. `total` is the
wall time inside the update pass; `next_step()` may add lock/logging overhead
outside it.

## Caveats

- These numbers are environment-specific. Compare runs on the same host and
  Python version.
- The offline baseline is not an end-to-end emulator tick: it excludes
  GoNetEm/gRPC, `tc`/netem, route installation, HIL, and traffic.
- The advanced model can drop links, so its ISL count can differ from the basic
  model (for example, 1,476 vs 1,512 at 1,008 satellites / 5 s).
- V5's published benchmark uses a different ground-link shape (roughly 482
  GSLs) and reports larger absolute tick times; do not compare the two tables
  directly.


## Phase 4 optical refresh

Phase 4 adds advanced optical constraint metrics to the per-tick refresh. A
post-change run on the same method gives:

| Case | Tick p50 | Tick p95 | `link_sync` p50 |
|---|---:|---:|---:|
| 720 basic, 5 s | 0.022 s | 0.023 s | 0.0026 s |
| 720 advanced, 5 s | 0.173 s | 0.217 s | 0.0085 s |
| 3,360 basic, 60 s | 0.155 s | 0.249 s | 0.0089 s |
| 3,360 advanced, 60 s | 0.700 s | 0.877 s | 0.0320 s |

Basic is unchanged within noise. Advanced `link_sync` grows because both ends'
constraint metrics are refreshed, but the V5 optical tick still dominates and
the totals stay inside the documented 5 s / 60 s budgets. At 1 s, 3,360
advanced remains over budget, as in the Phase 0 baseline.
