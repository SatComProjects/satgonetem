# Experiments, Telemetry, and the CLI

SatGoNetem ships a small experiment layer on top of the Python API: a YAML
scenario, a runner that writes reproducible artifacts, a metrics collector, and
the `satgonetem` command-line tool. The layer is intentionally thin; every
operation still goes through public `TopologyManager` methods.

## CLI

```bash
satgonetem version
satgonetem doctor
satgonetem summary topology_files/test_topology.json
satgonetem run scenario.yaml --out runs/001
satgonetem report runs/001
```

- `doctor` checks Python, Docker, the GoNetEm endpoint, and bundled topology
  data. It exits non-zero when a prerequisite fails.
- `summary` loads a topology without Docker and prints counts and link types.
- `run` starts a scenario. `--dry-run` validates and writes the manifest only.
  `--offline` loads and samples the topology without starting containers.
- `report` summarizes a run directory and prints the report path.

The console script is declared in `pyproject.toml` and installed as
`satgonetem`.

## Scenario format

```yaml
name: leo-handover-study
topology: starlink_topology.json   # bundled name or filesystem path
duration_s: 600
tick_s: 1                          # must be 1 when traffic is present
routing: static
network:                           # optional NetworkConfig overrides
  ipv6_enabled: true
  link_loss_percent: 0.1
traffic:
  - src: Gnd0
    dst: Gnd1
    tool: iperf3
    start_s: 10
    duration_s: 60
    protocol: TCP
events:
  - {at_s: 120, action: link_down, src: Sat12, dst: Sat13, duration_s: 30}
  - {at_s: 200, action: impairment, src: Sat12, dst: Sat13,
     loss_percent: 2.0, jitter_ms: 5}
metrics_interval_s: 1
connectivity: true                # run an all-pairs ping matrix at the end
```

Validation rejects unknown fields, unsupported traffic tools, malformed event
actions, and traffic with `tick_s != 1`. A link-down event is expanded into a
matching link-up event when `duration_s > 0`.

### Event semantics

- `link_down` / `link_up` set the link's local admin state and call
  `NodeSetIfState` on both endpoints. The topology's satcom-derived
  `is_active` flag is left untouched, and `Link.admin_up` prevents later ticks
  from overwriting the interface state.
- `impairment` updates loss/jitter and marks the link for a backend update.
- `node_restart` calls the raw GoNetEm `NodeRestart` RPC and clears the
  cached container/PID. Addresses, routes, and qdiscs must be reconfigured
  before traffic resumes; the runner records this as a known limitation.

### Time base

A live run with traffic defaults to real-time pacing: one tick per `tick_s`
wall-clock seconds, with `update_factor` left at 1.0. Without traffic, the
runner fast-forwards. `tick_s` must be 1 when traffic is present because
`FlowScheduler` and the traffic tools use wall-clock durations. Sub-second ticks
and deterministic replay are not part of this pass.

## Run artifacts

Each run directory contains:

| File | Contents |
|---|---|
| `manifest.yaml` | Validated scenario plus run directory and start time |
| `metrics.jsonl` | One metrics sample per interval |
| `flows.json` | Serialized ping/iperf3/hping3 results and errors |
| `connectivity.json` | Optional all-pairs connectivity matrix (`connectivity: true`) |
| `summary.json` | Machine-readable aggregate summary |
| `report.md` | Human-readable report |

Failed flows and simulator errors are recorded in the artifacts rather than
silently dropped.

## Metrics

`satgonetem.telemetry.MetricsCollector(topology).sample()` returns a dict with:

- UTC timestamp, simulation step, simulation seconds;
- satellite, ground-station, and link counts;
- per-link source/destination, type, active/admin state, delay, capacity,
  loss, jitter, tx, and rx (tx comes from `monitor_links()` when available);
- the most recent simulation error.

`JsonlMetricsWriter` appends samples; `write_json` writes pretty JSON. The
collector never raises because a monitoring file is missing.

## Connectivity matrix

`satgonetem.connectivity.build_connectivity_matrix(nodes)` pings every ordered
pair and returns typed `ConnectivityResult` objects with reachability, packet
loss, and RTT min/avg/max. `ConnectivityMatrix.to_dict()` and `to_json()`
serialize the result for reports or CI. `TopologyManager.connectivity_matrix()`
is the convenience wrapper for the current addressable nodes.

`test_connectivity()` remains as a printing wrapper for compatibility.

## Known limitations

- IPv6 is addressing-only; IPv6 routing and traffic are not implemented.
- The scenario runner does not yet use `FlowScheduler`'s shared `t0`; traffic
  therefore requires `tick_s=1` and real-time pacing.
- No deterministic seed/replay, pause/resume, or Prometheus endpoint yet.
- Capture and snapshot are exposed as raw backend controls; they are not
  integrated into the runner.
