# SatGoNetem Feature Ideas

- Status: Proposal / idea catalogue (not an approved specification)
- Date: 2026-11-22
- Basis: read-only scan of `fix/audit-medium` at `f6b4ccd`
- Purpose: capture features that would increase the research value and usability
  of SatGoNetem.

Each accepted idea needs its own spec and, where it changes architecture, an
ADR under `docs/adr/` before implementation, as required by `AGENTS.md`.

## Thesis

SatGoNetem already has a rare combination: a real Linux data plane, a dynamic
orbital topology, and pluggable routing. The highest-value next step is not
"more protocols". It is to make the existing realism **observable,
reproducible, and physically richer**:

1. turn the library into an experiment platform;
2. make links and failures behave like real satellite links;
3. add one flagship capability that few other emulators have (DTN or 5G NTN).

## What is already strong

- Container-per-node emulation with kernel `netem` + TBF QoS through GoNetEm.
- SGP4 and Walker constellation construction with per-tick link add/update/delete.
- Four routing methods: static Dijkstra, Bird OSPF, Bird IS-IS + SR, and kernel
  SR-MPLS. A P4/BMv2 daemon exists but is experimental and unregistered.
- Traffic tools: ping, iperf3, hping3, and `FlowScheduler`.
- RF link budget for ground-station links: antenna/EIRP, FSPL, optional ITU
  atmospheric attenuation, Shannon and DVB-S2X MODCOD strategies.
- Hardware-in-the-loop ground stations, an out-of-band controller plane, and
  deterministic IPv4 addressing.
- Topology persistence with embedded ground-station data.

## Value-limiting gaps

| Gap | Evidence | Consequence |
|---|---|---|
| No experiment layer | `services/topology_satcom.py:316-342` stores configuration, not live state; `traffic/flow_scheduler.py:133-139` schedules on wall-clock time | Runs are hard to reproduce, batch, or compare |
| Observability is a file scrape | `services/mixins/diagnostics.py:64,148-150` reads `/tmp/interfaces/...` and sets `link.rx = 0` | No unified metrics, time series, queue/drop data, or live health |
| Thin link model | `models/link.py:26-76`; `launchers/gonetem_client.py:31-32`; `launchers/gonetem_project.py:213-224` | No typed loss/jitter; generated YAML hardcodes `jitter: 0` |
| Budget only for GSLs | `models/link.py:82` | ISLs stay at static capacity; no Doppler or weather dynamics |
| Budget discards decisions and cannot model outage | `link_budget/service.py:89-159`; `models/link.py:214-262` | Selected MODCOD, C/N0, and losses are not observable; zero-capacity links fall back to the default capacity |
| Faults are one-shot | `services/mixins/simulation_loop.py:235-314` | No schedule, duration, recovery, node failure, or interface flap |
| Backend RPCs are unused | `proto/netem.proto` (`NodeSetIfState`, `NodeRestart`, `TopologyStopAll`, `NodeCapture`, `ProjectGetStatus`, `ProjectSave`) | Pause/restart/capture/health/snapshot require new code that the backend already supports |
| IPv4 unicast only | `addressing/ipv4.py`; `NetworkConfig.protocol` is a label | No IPv6, multicast, BGP, or public/NAT scenarios |
| No DTN | `routing/__init__.py:8-11` registers IP-route daemons only | No contact-graph routing or store-and-forward for sparse networks |
| Traffic is narrow | `satgonetem/traffic/` exposes ping, iperf3, and hping3 | No application profiles, PCAP replay, traffic matrices, or aggregate QoE |
| Packaging and UX | `pyproject.toml:67-68` lists only `packages = ["satgonetem"]`; no `[project.scripts]`; no `py.typed` | Wheels omit `topology_files/` and scenarios; no CLI; downstream type checkers ignore annotations |
| Routing is narrowly scoped | `routing/static_daemon.py`, `routing/srmpls_daemon.py` | Single shortest path, no ECMP, no capacity/queue-aware metrics, no BGP/SRv6/OSPFv3 |
| Coverage is a scalar | `utils/satellite_coverage.py:169-240` | No per-station visibility matrix, pass/contact plan, or handover schedule |
| CI is unit-only | `.github/workflows/ci.yml`; no integration tests use the declared marker | Docker/root/GoNetEm paths can regress unnoticed |

## Proposal catalogue

### P1 — Experiment platform (highest ROI)

#### P1.1 `satgonetem` CLI and `doctor`

Add a console entry point and commands:

```text
satgonetem run scenario.yml --out runs/001
satgonetem status
satgonetem shell Sat0
satgonetem capture Sat0 eth1 -o runs/001/cap.pcap
satgonetem report runs/001
satgonetem doctor
```

- Reuse `services/node_terminal.py` for headless shells, with the existing
  docker fallback used by the verification scripts.
- `doctor` checks Docker, the GoNetEm server, images, disk space, and protocol
  compatibility via `ServerGetVersion`.
- Add `[project.scripts]` to `pyproject.toml`.

**Value:** a first run measured in minutes instead of an 82-container quick
start; a supported surface for CI, notebooks, and demos.
**Complexity:** S–M.

#### P1.2 Experiment manifest, run directory, and reproducibility

One declarative file per experiment:

```yaml
name: leo-handover-study
topology: topology_files/starlink_topology.json
duration_sim_s: 3600
tick_s: 0.5
routing: static
seed: 42
traffic:
  - {src: Gnd0, dst: Gnd1, tool: iperf3, start_sim_s: 10, duration_s: 60}
events:
  - {at_sim_s: 120, action: link_down, src: Sat12, dst: Sat13, duration_s: 30}
metrics: [link.tx, link.rx, link.delay, qdisc.drops, flow.rtt, flow.loss]
```

Each run writes `manifest.yaml`, logs, metrics, captures, the final topology
state, and a report. Scheduling must use simulation time, not wall-clock time.
A seed and a deterministic clock are required for repeatability.

**Value:** the difference between a demo and a publishable experiment; enables
parameter sweeps and regression comparisons.
**Complexity:** M.

#### P1.3 Telemetry, exports, and a live API

- Collect per-link: `tx_bps`, `rx_bps`, `delay_ms`, capacity per direction,
  qdisc backlog/drops, and up/down state.
- Collect per-node: health, interfaces, routes, CPU/memory optionally.
- Collect per-flow: start/stop, goodput, RTT, loss, jitter, retransmits, exit
  status.
- Collect model state: selected MODCOD, C/N0, elevation, atmospheric loss,
  coverage, handover events.
- Export JSONL/CSV/Parquet and expose a Prometheus endpoint.
- Add a read-only FastAPI app (the dependency already exists) with `/health`,
  `/topology`, `/links`, `/metrics`, `/flows`, `/events`, and a WebSocket for
  live updates.
- Add a minimal live topology view: nodes colored by health, links by
  utilization, active routes and flows on selection.

Also fix the existing path: `Satellite.start_net_logger()` writes to a different
path/format than `monitor_links()` expects, and `monitor_links()` never measures
`rx`. `test_connectivity()` should return a per-pair RTT/loss matrix instead of
printing a summary.

**Value:** makes experiments debuggable and publishable; enables CI health
checks and dashboards.
**Complexity:** M.

#### P1.4 Wire the unused GoNetEm control plane

Expose the backend capabilities already present in `proto/netem.proto`:

| Capability | RPC | Use |
|---|---|---|
| Interface up/down | `NodeSetIfState` | Link flap with recovery, without deleting the link |
| Node stop/restart | `NodeStop`, `NodeRestart` | Node failure and recovery experiments |
| Suspend/resume all nodes | `TopologyStopAll`, `TopologyStartAll` | Experiment suspend (stops containers, not a true freeze) |
| Packet capture | `NodeCapture` | Streaming capture from any node, including ground stations |
| Health/status | `ProjectGetStatus` | Reconcile local flags with real node/interface state |
| Snapshot | `ProjectSave` | Save the live project archive for later analysis |

Map a `Link` to the backend interface index through
`Interface.get_iname()` (`eth<id>`). These are small wrappers with high value.

**Value:** pause/restart/capture/health/snapshot without new backend work.
**Complexity:** S–M.

#### P1.5 Package the scenario library and typing surface

- Fix package discovery and ship `topology_files/`, `resources/`, and example
  data in the wheel, or expose a downloader/`importlib.resources` accessor.
- Add `py.typed`.
- Add a single source of truth for the version.
- Add a docs CI job that checks links and extracts/runs code examples.
- Fix known copy-paste failures: the `TopologyManager(project_path=...)` example,
  the controller `main.py` reference, the missing ground-station CSV, and the
  unused `NetworkConfig` example.

**Value:** `pip install` users get the advertised scenarios; docs stop rotting.
**Complexity:** S–M.

### P2 — Physical and failure realism

#### P2.1 Typed link impairments and queueing

- Add a `LinkImpairment` dataclass and fields on `Link`/`NetworkConfig`:
  `loss`, `jitter_ms`, `delay_distribution`, `correlation`, and per-direction
  overrides.
- Emit impairments in both generated YAML and `_link_request`; preserve
  asymmetry in the initial project.
- Add per-link queue limits and AQM/DiffServ support (fq_codel, cake, HTB
  classes, DSCP/PCP classification).
- Start with loss/jitter because `LinkConfig.QoSConfig` already carries them.
  Corruption, duplication, reordering, and sub-millisecond delay need an
  upstream GoNetEm change.
- Expose `tc -s` counters (drops, backlog, requeues) through the telemetry
  layer.

**Value:** transport-protocol, congestion-control, QoS, and rain-fade studies
become credible.
**Complexity:** S for loss/jitter; M–L for AQM and upstream extensions.

#### P2.2 ISL link budget, MODCOD observability, and outage state

- Extend the link budget to RF/optical ISLs, not only GSLs.
- Add Doppler, elevation/range dependence, and rain/atmospheric time series.
- Return a structured result: capacity per direction, selected MODCOD, C/N0,
  elevation, FSPL, atmospheric loss, and a reason when no MODCOD closes.
- Model an explicit outage/zero-rate state instead of falling back to
  `default_capacity_kbps`.
- Feed capacity changes into routing weights and publish budget/MODCOD values
  as metrics.
- Preserve per-direction uplink/downlink asymmetry from initial wiring.

**Value:** LEO capacity variation, ACM, and link outages become observable and
affect routing.
**Complexity:** M.

#### P2.3 Fault and mobility event engine

Add a simulation-time event scheduler:

- link down/up and interface flap;
- node stop/restart and process failure;
- loss/jitter bursts and bandwidth degradation;
- weather/rain-fade events;
- repeat, probability, duration, and recovery policy.

Add mobile ground stations and user terminals, and record handover KPIs:
break time, packet loss, RTT spike, route convergence time. Implement the
documented `Satellite.handover` flag or remove the stale docstring. Add a
sub-second tick (`NetworkConfig.update_time` is currently an integer clamped to
at least 1 s) so handovers are meaningful.

**Value:** resilience, routing-convergence, and NTN mobility studies.
**Complexity:** M (event engine), L with mobility/sub-second timing.

#### P2.4 Sub-second timing and delay precision

- Allow `update_time` as a float with a practical floor (e.g. 0.1 s).
- Support microsecond/float delay in the Python model and, if possible, in
  GoNetEm; the current protocol uses integer milliseconds and a 1 ms floor.
- Separate simulation time from wall-clock pacing and expose drift.

**Value:** LEO ISL delays of a few milliseconds, handovers, and transport
experiments need finer granularity than 1 s / 1 ms.
**Complexity:** M, plus upstream coordination.

### P3 — Research differentiators

#### P3.1 DTN and contact-graph routing

- Generate a contact plan from the orbital model: contacts, start/end times,
  data rates, and ranges.
- Add a contact-graph routing (CGR) daemon and a store-and-forward queue model.
- Integrate BPv7/LTP directly or provide an adapter to an external DTN stack.
- Add metrics: bundle delivery ratio, delivery latency, queue occupancy, and
  contact utilization.
- Export/import contact plans for interoperability.

**Value:** the strongest unique capability for sparse, intermittent, and
deep-space networks; current routing assumes a connected IP graph.
**Complexity:** L.

#### P3.2 IPv6, multicast, and inter-domain routing

- Add an IPv6/dual-stack address plan and IPv6 link/loopback addressing.
- Add OSPFv3 and IS-IS IPv6, or a dual-stack variant of the existing daemons.
- Add PIM-SM/SSM multicast for group delivery and DVB-style broadcast.
- Add BGP for ground-segment and inter-constellation routing.
- Consider public address pools and NAT for Internet-egress experiments.

**Value:** modern constellation, 5G NTN, and media-delivery experiments.
**Complexity:** L.

#### P3.3 Application traffic, replay, and aggregate analysis

- Add traffic profiles: HTTP, DASH/video, VoIP, QUIC, DNS, bulk transfer, and
  synthetic Poisson/Pareto arrivals.
- Add PCAP replay (`tcpreplay` or an equivalent) and a traffic-matrix scheduler.
- Support long-lived servers and per-interface/address binding.
- Fix `Iperf3Config` validation (`protocol` silently falls back to TCP) and the
  documented-but-missing `ipv6` option; add an actual iperf2 runner or remove
  the parsing-only surface.
- Add aggregate results: CSV/JSONL export, per-run statistics, confidence
  intervals, and plots across flows and time steps.

**Value:** QoE, congestion, and application-level experiments instead of only
raw transport tests.
**Complexity:** M.

#### P3.4 5G NTN and HIL expansion

- Integrate an open 5G core (Open5GS/Free5GC) with gNB/UE containers, N2/N3,
  QoS flows, and network slices.
- Add PTP/time-sync and Doppler models for NTN timing.
- Expand HIL to user terminals and SDR-based links; shape uplink traffic too.
  The current HIL path shapes only satellite-side egress and keys bridges by
  ground-station ID, limiting concurrent links.
- Support multi-link HIL per ground station.

**Value:** industry-relevant 5G NTN and hardware experiments.
**Complexity:** XL.

#### P3.5 Scale and performance

- Add a lightweight netns/host launcher and a benchmark suite at 1k+ nodes.
- Measure per-tick cost, route-install time, memory, and container startup.
- Reduce full route recomputation and add incremental routing updates.
- Current evidence: the live 10×10 script reaches 105 containers; the
  900-satellite script validates addressing only.

**Value:** mega-constellation studies and credible performance claims.
**Complexity:** L.

#### P3.6 Visualization, constellation design, and interoperability

- 3D/live view of satellites, ground stations, active links, routes, and flows.
- Constellation optimizer over coverage, latency, capacity, cost, and
  handover count.
- Per-ground-station visibility matrix, pass/contact plan, and access-duration
  statistics.
- Export to ns-3/OMNeT++/STK/Hypatia; import TLEs and contact plans.

**Value:** teaching, demos, design-space exploration, and adoption in existing
workflows.
**Complexity:** M–L.

## Quick wins

| Change | Where | Why |
|---|---|---|
| Add `loss`/`jitter` to `Link`, `NetworkConfig`, YAML, and `_link_request` | `models/link.py`, `launchers/` | Proto already supports it; immediate realism gain |
| Expose `NodeSetIfState`, `NodeRestart`, `NodeCapture`, `ProjectGetStatus`, `ProjectSave` | `services/mixins/` | Backend already supports them |
| Add a CLI entry point and `doctor` | `pyproject.toml`, new `cli/` | Removes the largest onboarding barrier |
| Return a structured connectivity matrix from `test_connectivity()` | `services/topology_satcom.py` | Makes the existing check useful for CI/reporting |
| Fix `start_net_logger`/`monitor_links` mismatch and measure `rx` | `models/satellite.py`, `services/mixins/diagnostics.py` | The current observability path is not wired |
| Ship `topology_files/` and add `py.typed` | `pyproject.toml` | Installed users cannot load the advertised scenarios |
| Fix iperf3 option validation and docs (`ipv6`) | `traffic/iperf3_config.py`, `docs/traffic.md` | Avoid silent TCP fallback and broken examples |
| Add a BFD protocol block or remove `bfd yes` | `routing/isis_sr_bird_daemon.py` | Prevent an invalid Bird config |
| Decide P4's fate: support/register or remove | `routing/p4_daemon.py` | Avoid dead experimental code |
| Add docs link/example CI | `.github/workflows/` | Stop copy-paste rot |

## Suggested sequencing

**Wave 1 — Usable experiments**

- CLI + `doctor`; experiment manifest and run directory.
- Telemetry export, connectivity matrix, and basic report.
- Unused GoNetEm RPCs (interface flap, node restart, capture, status).
- Loss/jitter fields; packaging/scenario fixes.

**Wave 2 — Credible physics**

- Fault/mobility event engine and handover KPIs.
- ISL budget, MODCOD observability, outage state, Doppler/weather.
- AQM/DiffServ and queue statistics.
- Sub-second tick and finer delay.
- Live dashboard on top of the API.

**Wave 3 — Flagship scope**

- Choose DTN/CGR or 5G NTN as the flagship.
- Then IPv6/multicast/BGP, application traffic/replay, and scale.
- Visualization, constellation optimizer, and interoperability.

## Success metrics

- Time from install to first successful ping/iperf run under 10 minutes.
- A scenario rerun with the same seed produces the same topology/event timeline
  and metrics within documented tolerances.
- Every advertised sample topology loads from an installed wheel.
- At least one end-to-end integration test per routing backend and for
  HIL/controller paths, run on a privileged/scheduled CI lane.
- A 1k-node benchmark with published per-tick and route-install costs.
- Every feature has a spec, acceptance tests, and a docs update.

## Guardrails

- Finish the H1–H13 audit on `fix/audit-medium` first. Building features on
  known link-state and persistence defects creates rework.
- Follow the `AGENTS.md` spec-first/TDD workflow; one concern per change.
- Do not add more routing daemons before telemetry. You cannot prove they work.
- Build the API before the GUI; the API also serves CI, notebooks, and scripts.
- Do not weaken tests, skip Docker/root prerequisites silently, or game
  coverage.
- Upstream GoNetEm changes (corruption/reordering, sub-ms delay, richer QoS,
  link statistics) need coordination and a compatibility plan.

## Non-goals

- Replacing a full 5G core or a production satellite operations centre.
- Bit-exact packet-level simulation; SatGoNetem's value is kernel-level
  emulation.
- Adding every routing protocol; depth in a few protocols plus observability is
  more valuable.
- A polished GUI before the experiment API and metrics exist.

## Open questions

1. Who is the primary user: academic researchers, students/teaching, or
   industry NTN teams?
2. Is the flagship differentiator DTN/CGR or 5G NTN?
3. What scale target matters: 100, 1,000, or 10,000 nodes?
4. Can GoNetEm be extended upstream, or must all changes stay client-side?
5. Is PyPI distribution a goal? If so, `sat_com_topology` must be published or
   vendored.
6. Is HIL hardware available for regular integration testing?

## Evidence index

- Core lifecycle and tick pipeline: `services/topology_satcom.py`,
  `services/mixins/network_lifecycle.py`, `services/mixins/simulation_loop.py`,
  `services/mixins/topology_sync.py`, `services/mixins/link_ops.py`.
- Observability/diagnostics: `services/mixins/diagnostics.py`,
  `models/satellite.py`, `utils/satellite_coverage.py`.
- Link/QoS/budget: `models/link.py`, `launchers/gonetem_client.py`,
  `launchers/gonetem_project.py`, `link_budget/`.
- Routing: `routing/registry.py`, `routing/static_daemon.py`,
  `routing/ospf_daemon.py`, `routing/isis_sr_bird_daemon.py`,
  `routing/srmpls_daemon.py`, `routing/p4_daemon.py`.
- Traffic: `traffic/`, `services/mixins/traffic_testing.py`.
- Addressing: `addressing/ipv4.py`.
- Backend protocol: `proto/netem.proto`, `docs/proto.md`.
- Packaging/CI: `pyproject.toml`, `.github/workflows/ci.yml`,
  `.github/workflows/release.yml`.
- Existing debt/audit: `bugs.md` (git-local, never commit), `docs/specs/`,
  `docs/refactor_plan.md`.
