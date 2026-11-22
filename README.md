# SatGoNetem

[![CI](https://github.com/SatComProjects/satgonetem/actions/workflows/ci.yml/badge.svg)](https://github.com/SatComProjects/satgonetem/actions/workflows/ci.yml)

**Satellite Network Emulation Tool** - A Python framework for emulating dynamic satellite constellations (LEO, MEO, GEO) as live virtual networks with real-time topology updates, pluggable routing, and traffic measurement.

SatGoNetem bridges orbital mechanics and Linux network emulation. It computes satellite positions using SGP4 propagation (or walker constellation patterns), spawns Docker containers for each node, wires them together with virtual ethernet pairs, and applies `tc netem`/TBF qdiscs to model propagation delay and link capacity. As orbits evolve, the emulated topology updates accordingly.

## Documentation

**Getting started**

- [`docs/project_creation.md`](docs/project_creation.md) — End-to-end constellation design guide
- [`docs/custom_project.md`](docs/custom_project.md) — TLE-only and mixed custom projects outside Walker shell generation
- [`docs/topology_manager_api.md`](docs/topology_manager_api.md) — `TopologyManager` public API reference
- [`docs/experiment.md`](docs/experiment.md) — CLI, YAML scenarios, run artifacts, telemetry, and connectivity matrix

**Core internals**

- [`docs/models.md`](docs/models.md) — Node, satellite, ground station, interface, link, routing, and MPLS models
- [`docs/services.md`](docs/services.md) — `NetworkConfig`, mixin composition, initialization order, tick pipeline, locking
- [`docs/addressing.md`](docs/addressing.md) — IPv4 pools, node blocks, loopbacks, and link allocation
- [`docs/addressing_plan.md`](docs/addressing_plan.md) — `Ipv4AddressPlan` API, validation, and slot reuse

**Backends and protocol**

- [`docs/launchers.md`](docs/launchers.md) — `NetworkLauncher` contract, GoNetEm backend, generated project format, QoS
- [`docs/hil_manager.md`](docs/hil_manager.md) — Hardware-in-the-loop integration
- [`docs/controller.md`](docs/controller.md) — Out-of-band controller/management plane
- [`docs/proto.md`](docs/proto.md) — GoNetEm gRPC service, messages, and compatibility

**Networking and measurement**

- [`docs/routing.md`](docs/routing.md) — Routing daemon architecture and custom daemon registration
- [`docs/traffic.md`](docs/traffic.md) — Ping, iperf3, hping3 tools and `FlowScheduler`
- [`docs/link_budget.md`](docs/link_budget.md) — Link budget calculations and parameters

**Support**

- [`docs/utils.md`](docs/utils.md) — Project builder, geodesy/coverage helpers, connection strategies, shared memory, compatibility patch
- [`docs/ports.md`](docs/ports.md) — External-boundary ports, adapters, and injection points

**Development**

- [`AGENTS.md`](AGENTS.md) — Development principles, six-stage workflow, branch policy, required commands
- [`docs/specs/`](docs/specs/README.md) — Phase specifications, acceptance criteria, and verification
- [`docs/adr/`](docs/adr/README.md) — Architecture decision records

## Features

- Real-time satellite constellation simulation (LEO/MEO/GEO, walker and TLE-based)
- Docker container-per-node with kernel-level netem delay and rate shaping
- Hardware-in-the-loop (HIL) support for bridging physical ground station equipment
- Pluggable routing backends: static (Dijkstra), OSPF (Bird2), IS-IS SR (patched Bird2), SR-MPLS
- Built-in traffic tools: ping, iperf3, hping3 with structured result parsing
- Batch traffic scheduling via `FlowScheduler`
- Interactive scenario shell (`satgonetem create` / `edit`) for Walker shells,
  ground stations, traffic, and events
- Declarative YAML experiments with run artifacts, telemetry, and reports
- Optional dual-stack IPv6 addressing alongside the deterministic IPv4 plan
- Per-link loss and jitter fields
- Python API for constellation design and programmatic control
- Supports Python 3.10 through 3.14

## Requirements

- Linux (kernel netlink operations require a Linux host)
- Docker daemon running and accessible
- GoNetEm
- Python >= 3.10

## Installation

```bash
pip install git+https://github.com/satcomprojects/satgonetem
```

The primary external dependency is `sat_com_topology`:

```
git+https://github.com/SatComProjects/satComTopology.git@v5.0.3
```

The repository is public; pip fetches it from GitHub at install time, so an
internet connection is required.

For Link Budget extras:
```bash
pip install -e ".[extra]"
```

For development extras (linting, type checking, coverage):

```bash
pip install -e ".[dev]"
```

### GoNetEm

The `GoNetEmLauncher` backend delegates network operations to a running `gonetem-server` daemon via gRPC. SatGoNetEm is developed against the [SatComProjects fork of GoNetEm](https://github.com/SatComProjects/gonetem).

Clone and install from source:

```bash
git clone https://github.com/SatComProjects/gonetem
cd gonetem
sudo make install-amd64   # or install-arm64
gonetem-console pull
```

`make install-*` builds `gonetem-server` and `gonetem-console`, installs them under `/usr/local/bin`, installs the default configuration at `/etc/gonetem/config.yaml` (including the `jariassuarez/sgnt` satellite and ground-station images), and starts `gonetem.service`. `gonetem-console pull` fetches every Docker image that configuration references.

The server listens on `localhost:10110` by default, which matches the `gonetem_server` default in `NetworkConfig`. `TopologyManager.start_gonetem()` constructs the GoNetEm launcher automatically; `network_launcher="GONETEM"` is recorded in `NetworkConfig` and serialized with the project. See [`docs/launchers.md`](docs/launchers.md) for the launcher contract and lifecycle.

## Quick Start

### Using a pre-configured test constellation

```python
from satgonetem.utils.project_builder import create_test_project
from satgonetem.services.topology_satcom import TopologyManager

# Iridium-like constellation: 77 satellites, 5 European ground stations
project = create_test_project()
topology = TopologyManager.from_satcom(project)

topology.start_gonetem()         # starts containers and wires the network
topology.set_ip_addresses()      # push the planned IPv4 addresses into containers
topology.init_routing("static")  # Dijkstra shortest-path routing
topology.start()                 # begins simulation loop (topology updates every tick)
```

### Defining a custom constellation

```python
from satgonetem.utils.project_builder import (
    GroundStationEntry,
    GroundObjectFile,
)
from sat_com_builder.models import (
    SimulationProperty,
    GroundObjectProperty,
    GroundConnectivityProperty,
    WalkerShellProperty,
    OrbitalConnectivityProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty

# Ground stations
entries = [
    GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
    GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
]
gs_file = GroundObjectFile("Ground Stations", entries)
ground_obj = GroundObjectProperty(
    identifier=gs_file.identifier,
    data_file=gs_file.write("/tmp"),
    type="ground_station",
    connectivity_properties=GroundConnectivityProperty(
        ground_to_space_connections_strategy="best-angle-until-disconnection",
        elevation_above_horizon=10,
        maximum_satellite_range_distance=1500.0,
        shell_white_lists=["LEO"],
        maximum_connected_satellites=3,
    ),
)

# Walker shell (LEO)
shell = WalkerShellProperty(
    type="star",
    constellation_property=WalkerConstellationProperty(
        identifier="LEO",
        amount_of_orbit_plane=7,
        amount_of_satellite_per_orbit_plane=11,
        inclination=86.4,
        mean_revolution_per_day=14.35,
        phase_difference_between_satellites=True,
    ),
    orbital_connectivity_property=OrbitalConnectivityProperty(...),
    ground_object_white_list=["Ground Stations"],
)

project = SimulationProperty(
    simulation_name="MyNet",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 00:10:00",
    walker_shells=[shell],
    ground_objects_properties=[ground_obj],
)

topology = TopologyManager.from_satcom(project)
```

### Running traffic measurements

```python
from satgonetem.traffic import PingConfig, Iperf3Config
import time

src = topology.get_node_by_name("Sat0")
dst = topology.get_node_by_name("Gnd0")

# ICMP ping
flows = topology.ping(src, dst, PingConfig(count=10))
for flow in flows:
    flow.join()
    print(flow.results().rtt_avg_ms)

# TCP throughput
flows = topology.run_iperf3(src, dst, Iperf3Config(protocol="TCP", duration=10))
for flow in flows:
    flow.join()
    print(flow.results().avg_throughput_mbps)
```

### Teardown

```python
topology.stop()          # stop simulation loop
topology.stop_gonetem()  # remove containers, veth pairs, and qdiscs
```

### Using the CLI

```bash
satgonetem doctor
satgonetem summary topology_files/test_topology.json
satgonetem create my-scenario
satgonetem edit my-scenario.yaml
satgonetem run scenario.yaml --out runs/001
satgonetem report runs/001
```

`create` starts an interactive scenario shell (Walker shells, ground stations,
traffic, events, network overrides) and writes a scenario file; `edit` opens an
existing scenario in the same shell. `run` accepts the resulting YAML scenario.
Use `--dry-run` to validate without loading the topology and `--offline` to
sample without starting containers. See
[`docs/experiment.md`](docs/experiment.md) for the schema and limitations.

## Architecture

```
satgonetem/
  models/        Node, Satellite, GroundStation, Link, Interface
                 MPLS entries, routing entries, sat_com Pydantic models
  launchers/     NetworkLauncher (abstract) + GoNetEmLauncher (gRPC)
                 HILManager (hardware-in-the-loop)
  routing/       RoutingDaemon (abstract) + static, OSPF, ISIS-SR, SR-MPLS
  traffic/       Ping, iperf2, iperf3, hping3 traffic tools and FlowScheduler
  link_budget/   Antenna, transmitter, receiver, propagation, MODCOD, geometry
  addressing/    Deterministic IPv4 pools and optional IPv6/ULA pools,
                 node blocks, loopbacks, and point-to-point links
  services/      TopologyManager (central orchestrator)
                 mixins: topology sync, link ops, routing mgr, interface mgr,
                         network lifecycle, traffic testing, simulation loop,
                         diagnostics, controller
  utils/         project_builder, IP helpers, custom connection strategies
  proto/         gRPC definitions for the GoNetEm backend
```

**TopologyManager** (`satgonetem/services/topology_satcom.py`) is the main entry point. It is composed of mixins under `services/mixins/` that each handle a specific concern: syncing topology data from the orbital simulator, managing Docker containers and virtual interfaces, applying `tc` qdiscs, configuring routing daemons, running traffic tests, and driving the simulation tick loop.

**Delay model:** `delay_ms = distance_km / 299792.458 * 1000` (propagation at speed of light).

**QoS stack per link:** `netem` (propagation delay) + `TBF` (rate limiting), configured by GoNetEm. The Python client batches per-tick changes through `LinkUpdateMany` (512 links per call) and falls back to per-link updates on older servers.

## IPv4 and IPv6 Addressing

Addresses come from a deterministic, private `10.0.0.0/8` plan by default:

| Purpose | Pool | Per-node block | Capacity |
|---------|------|----------------|----------|
| Ground stations | `10.0.0.0/10` | `/20` | 1,024 |
| User terminals | `10.64.0.0/10` | `/20` | 1,024 |
| Satellites | `10.128.0.0/9` | `/27` | 262,144 |

Each node has a stable `/32` loopback at `<block>.0.1`; traffic and static routes target that loopback, not an interface. Every point-to-point link gets a `/31` starting at `<block>.0.4`. Ground stations and user terminals own their links; satellite links are owned by the lower satellite ID. See [`docs/addressing.md`](docs/addressing.md) for the link-ownership rules and configuration.

Optional dual-stack IPv6 addressing is available with
`NetworkConfig(ipv6_enabled=True)`. It uses private ULA pools
(`fd00:1::/40` ground stations, `fd00:2::/40` satellites, `fd00:3::/40` user
terminals), `/64` node blocks, `/128` loopbacks, and `/127` point-to-point
links. IPv6 addressing is independent of the IPv4 plan; IPv6 routing and
traffic are not implemented yet.

The pools are configurable via `NetworkConfig`:

```python
config = NetworkConfig(
    ground_station_pool="10.0.0.0/10",
    satellite_pool="10.128.0.0/9",
    user_terminal_pool="10.64.0.0/10",
    ground_station_prefix_len=20,
    satellite_prefix_len=27,
    user_terminal_prefix_len=20,
)
```

## Routing Methods

| Method | Class | Description |
|--------|-------|-------------|
| `static` | StaticRoutingDaemon | Dijkstra over NetworkX graph, `ip route` applied per node |
| `dynamic-ospf` | OSPFDaemon | Bird2 daemon, per-node config templated via Jinja2 |
| `dynamic-isis` | ISISBirdSRDaemon | Bird2 IS-IS with SR-MPLS; needs a Bird build with IS-IS/MPLS |
| `sr-mpls` | SRMPLSDaemon | Segment Routing with MPLS labels |

`dynamic-isis` requires a Bird build with the IS-IS and MPLS protocols. The
shipped `jariassuarez/sgnt:satellite` and `:ground-station` images ship stock
Bird 2.x, which has neither; the daemon now fails fast with an actionable
error instead of writing an unusable configuration.

Custom routing daemons can be registered before initialization:

```python
TopologyManager.register_routing_daemon("my-method", MyDaemon)
topology.init_routing("my-method")
```

See `docs/routing.md` for the `RoutingDaemon` interface.

## Pre-configured Projects

The `topology_files/` directory contains ready-to-use JSON topologies for:

- **Iridium** - Real Iridium constellation parameters with European ground stations
- **Iris2** - ESA Iris2 constellation parameters
- **Starlink** - Starlink constellation parameters
- **Kuiper** - Amazon Kuiper constellation parameters
- **OneWeb** - OneWeb constellation parameters

Load a pre-configured topology directly with `TopologyManager`:

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_file("topology_files/iridium_topology.json")
```

These files are self-contained (ground station data is embedded) and can be moved or shared without additional dependencies.

## Configuration Reference

Key parameters on `TopologyManager`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `update_time` | 5 | Simulation tick interval in seconds |
| `routing` | `"static"` | Routing method name |
| `isl_link_capacity` | 100000 | Inter-satellite link capacity in kbps |
| `gnd_link_capacity` | 100000 | Ground-space link capacity in kbps |
| `ground_object_link_capacity` | 100000 | Ground-object link capacity in kbps |
| `link_netem_limit` | 1000 | netem queue limit of emulated links, in packets (`0` = server default) |
| `link_txqueuelen` | 1000 | Transmit queue length of emulated link interfaces, in packets (`0` = server default) |
| `link_loss_percent` | 0.0 | Default packet loss percentage for new links |
| `link_jitter_ms` | 0 | Default netem jitter for new links, in milliseconds |
| `satellite_image` | `"jariassuarez/sgnt:satellite"` | Docker image used for satellite containers |
| `network_launcher` | `"GONETEM"` | Recorded launcher backend identifier; GoNetEm is the implemented backend |
| `gonetem_server` | `"localhost:10110"` | GoNetEm gRPC server address |
| `use_budget` | `False` | Compute ground-station link capacity from the link budget |
| `ground_station_pool` | `10.0.0.0/10` | Pool subdivided into ground-station `/20` blocks |
| `satellite_pool` | `10.128.0.0/9` | Pool subdivided into satellite blocks |
| `user_terminal_pool` | `10.64.0.0/10` | Pool subdivided into user-terminal `/20` blocks |
| `ground_station_prefix_len` | 20 | Prefix length of each ground-station block |
| `satellite_prefix_len` | 27 | Prefix length of each satellite block |
| `user_terminal_prefix_len` | 20 | Prefix length of each user-terminal block |
| `ipv6_enabled` | `False` | Enable optional dual-stack IPv6 addressing |
| `ipv6_ground_station_pool` | `fd00:1::/40` | IPv6 ULA pool for ground-station `/64` blocks |
| `ipv6_satellite_pool` | `fd00:2::/40` | IPv6 ULA pool for satellite `/64` blocks |
| `ipv6_user_terminal_pool` | `fd00:3::/40` | IPv6 ULA pool for user-terminal `/64` blocks |

See [`docs/services.md`](docs/services.md#networkconfig) for every `NetworkConfig` field.

## Testing

```bash
pytest tests/ -v
```

The default suite excludes `slow` and `benchmark` tests. Run the extended set
explicitly:

```bash
pytest tests/ -q -m "slow or benchmark"
```

Tests cover models, routing daemons, traffic result parsing, launchers, and
project builder utilities. Unit tests use shared fixtures and fakes from
`tests/support/` and do not require Docker, root, or live networking; the
manual checks below exercise real emulation.

Scale and live connectivity checks:

```bash
# Address-plan scale: 900 satellites, 5 GS, 20 UTs, 2,900 unique /31 links
python scripts/verify_900_satellites.py

# Live three-node topology: GSL + ISL, static routing, bidirectional pings
python scripts/verify_connectivity.py

# Same, when GoNetEm NodeRun cannot exec into containers in the environment
python scripts/verify_connectivity.py --transport docker
```

## Continuous integration and releases

Two GitHub Actions workflows live in [`.github/workflows/`](.github/workflows/):

| Workflow | Trigger | What it runs |
| --- | --- | --- |
| [`ci.yml`](.github/workflows/ci.yml) | Push to `main` or `develop`, every pull request, manual dispatch | Ruff lint/format, full mypy, lockfile check, unit tests on Python 3.10–3.14 with a 75% coverage floor, dependency audit, sdist/wheel build, metadata validation, wheel install smoke test |
| [`release.yml`](.github/workflows/release.yml) | Push of a `v*` tag, manual dispatch | Lint/format, full mypy, tests and coverage, checks the tag matches `pyproject.toml`, builds the distributions, and publishes a GitHub Release with the sdist and wheel attached |

All jobs run on `ubuntu-latest` with [`uv`](https://docs.astral.sh/uv/) and the
committed `uv.lock`. No repository secrets are required: the test suite mocks
Docker and network operations, and `sat_com_topology` is public.

Run the same checks locally:

```bash
uv sync --extra dev                      # install the locked development environment
uv lock --check                          # pyproject.toml and uv.lock agree
uv run --extra dev ruff check satgonetem tests
uv run --extra dev ruff format --check satgonetem tests
uv run --extra dev mypy
uvx pip-audit@2.9.0 --path .venv/lib/python3.14/site-packages
uv run --extra dev pytest -q --cov=satgonetem --cov-fail-under=75
uv build --out-dir dist                  # sdist + wheel
uvx twine@7.0.0 check dist/*
```

### Required checks

The `develop` and `main` repository rulesets require pull requests and the
same checks: `Lint`, `Type check`, `Test (Python 3.10)` through
`Test (Python 3.14)`, `Security audit`, and `Build package`.

### Technical debt

Known follow-ups and intentional exceptions are tracked here and in
[`docs/specs/`](docs/specs/README.md):

| Area | Debt | Next step |
|---|---|---|
| Type checking | `attr-defined` is disabled globally because generated protobuf and mixin-injected state are not statically visible | Add protocols/annotations and re-enable the category |
| Coverage | `utils/project_mesh.py` (~30%) and `launchers/gonetem_lifecycle.py` (~58%) are below the global floor's spirit | Add mesh-construction and lifecycle branch tests |
| Behavior | Link-operation counts mean “attempted”, not “succeeded”; `FlowScheduler` defaults to no timeout | Separate behavior specs and tests |
| Controller | Custom interface patterns must format to `eth<digits>` | Decide whether arbitrary prefixes are needed |
| P4 | `P4Daemon` is experimental and not registered as a routing method | Support or remove it explicitly |
| Compatibility | `satcom_fix` patches third-party internals at import time | Separate removal spec if upstream fixes land |
| Integration | Docker/root/GoNetEm paths run only via manual scripts, not CI | Add a privileged integration runner when available |

### Releasing

1. Bump `version` in `pyproject.toml` and merge it through a green CI run.
2. Tag the release commit and push the tag:

   ```bash
   git tag -a v0.3.1 -m "SatGoNetem 0.3.1"
   git push origin v0.3.1
   ```

3. `release.yml` verifies that the tag matches `pyproject.toml`, builds `dist/`,
   and publishes a GitHub Release with generated notes and the artifacts attached.
4. Install a release directly from GitHub:

   ```bash
   pip install "satgonetem @ git+https://github.com/SatComProjects/satgonetem@v0.3.1"
   ```

`release.yml` does not publish to PyPI. `pyproject.toml` depends on
`sat_com_topology` through a direct Git URL, and PyPI rejects distributions whose
`Requires-Dist` contains a direct reference. To publish to PyPI later, first
publish `satComTopology` to PyPI and change the dependency to a versioned
requirement such as `sat_com_topology>=5.0.3`, then add a Trusted Publishing job
(`id-token: write`, `environment: pypi`).

Dependency and action updates are opened weekly by Dependabot
([`.github/dependabot.yml`](.github/dependabot.yml)).

## Citation

If you use this project in your research, please cite:

J. Arias Suarez, L. Barbier, M. Dehez-Clementi and O. Hotescu, "SatGoNetEm: an Open Source Emulator of Satellite Constellations and Non-Terrestrial Networks," 2026 IFIP Networking Conference (IFIP Networking), Lugano, Switzerland, 2026, pp. 1-10, doi: 10.23919/IFIPNetworking70592.2026.11579099. keywords: {Joining processes;Satellites;Modeling;Containers;Emulation;Timing;Grounding;Routing;Low earth orbit satellites;Printing;LSN;network emulation;Docker containers}

```bibtex
@INPROCEEDINGS{11579099,
  author={Arias Suarez, Juan and Barbier, Louis and Dehez-Clementi, Marina and Hotescu, Oana},
  booktitle={2026 IFIP Networking Conference (IFIP Networking)},
  title={SatGoNetEm: an Open Source Emulator of Satellite Constellations and Non-Terrestrial Networks},
  year={2026},
  volume={},
  number={},
  pages={1-10},
  keywords={Joining processes;Satellites;Modeling;Containers;Emulation;Timing;Grounding;Routing;Low earth orbit satellites;Printing;LSN;network emulation;Docker containers},
  doi={10.23919/IFIPNetworking70592.2026.11579099}}
```

## License

GPLv3 - see `pyproject.toml` for author and contact information.
