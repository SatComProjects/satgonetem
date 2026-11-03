# SatGoNetem

**Satellite Network Emulation Tool** - A Python framework for emulating dynamic satellite constellations (LEO, MEO, GEO) as live virtual networks with real-time topology updates, pluggable routing, and traffic measurement.

SatGoNetem bridges orbital mechanics and Linux network emulation. It computes satellite positions using SGP4 propagation (or walker constellation patterns), spawns Docker containers for each node, wires them together with virtual ethernet pairs, and applies `tc netem`/TBF qdiscs to model propagation delay and link capacity. As orbits evolve, the emulated topology updates accordingly.

## Documentation

- [`docs/topology_manager_api.md`](docs/topology_manager_api.md) — Full `TopologyManager` API reference
- [`docs/routing.md`](docs/routing.md) — Routing daemon architecture and custom daemon registration
- [`docs/traffic.md`](docs/traffic.md) — Ping, iperf3, hping3 tools and `FlowScheduler`
- [`docs/project_creation.md`](docs/project_creation.md) — End-to-end constellation design guide
- [`docs/hil_manager.md`](docs/hil_manager.md) — Hardware-in-the-loop integration
- [`docs/link_budget.md`](docs/link_budget.md) — Link budget calculations and parameters
- [`docs/addressing.md`](docs/addressing.md) — IPv4 pools, node blocks, loopbacks, and link allocation
- [`docs/performance.md`](docs/performance.md) — Topology update stage timings and the Phase 0 baseline
- [`docs/events.md`](docs/events.md) — Event-driven link sync, async ticks, and the Phase 2 decision
- [`docs/optical.md`](docs/optical.md) — Optical ISLs, temporary unavailability, and SSO/inter-shell links

## Features

- Real-time satellite constellation simulation (LEO/MEO/GEO, walker, SSO, and TLE-based)
- Optical ISLs with basic/advanced models, terminals, and inter-shell links
- Docker container-per-node with kernel-level netem delay and rate shaping
- Hardware-in-the-loop (HIL) support for bridging physical ground station equipment
- Pluggable routing backends: static (Dijkstra), OSPF (Bird2), IS-IS SR (patched Bird2), SR-MPLS
- Built-in traffic tools: ping, iperf3, hping3 with structured result parsing
- Batch traffic scheduling via `FlowScheduler`
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

Access to that repository is required at install time.

For Link Budget extras:
```bash
pip install -e ".[extra]"
```

For development extras (linting, type checking, coverage):

```bash
pip install -e ".[dev]"
```

### GoNetem

The `GoNetEmLauncher` backend delegates network operations to a running `gonetem-server` daemon via gRPC.

Clone and build from source:

```bash
git clone https://github.com/mroy31/gonetem
cd gonetem
make build-amd64   # or armv7 / arm64
```

If using the stock gonetem version, make sure to copy the configuration file to use SGNT's custom docker images:
```
sudo cp resources/gonetem_config/config.yaml /etc/gonetem/config.yaml

sudo service gonetem restart # If running

docker pull jariassuarez/sgnt:satellite
docker pull jariassuarez/sgnt:ground-station
```

The compiled binaries are placed in `bin/`. Pull the Docker images GoNetem uses internally:

```bash
./bin/gonetem-console pull
```



Start the server (requires root):

```bash
sudo ./bin/gonetem-server
```

The server listens on `localhost:10110` by default, which matches the `gonetem_server` default in `NetworkConfig`. To use this backend, set `network_launcher="GONETEM"` when constructing `TopologyManager`.

or a modified, satgonetem-optimized version:
```bash
git clone https://github.com/jariassuarez/gonetem
cd gonetem
make install-amd64   # or armv7 / arm64

gonetem-console pull
docker pull jariassuarez/sgnt:satellite
docker pull jariassuarez/sgnt:ground-station

```



## Quick Start

### Using a pre-configured test constellation

```python
from satgonetem.utils.project_builder import create_test_project
from satgonetem.services.topology_satcom import TopologyManager

# Iridium-like constellation: 77 satellites, 5 European ground stations
project = create_test_project()
topology = TopologyManager.from_satcom(project)

topology.set_ip_addresses()
topology.start_gonetem()         # starts containers and wires the network
topology.init_routing("static")  # Dijkstra shortest-path routing
topology.start()                 # begins simulation loop (topology updates every tick)
```

### Defining a custom constellation

```python
from sat_com_builder.models import (
    GroundObjectData,
    SimulationProperty,
    GroundObjectProperty,
    GroundConnectivityProperty,
    WalkerShellProperty,
    OrbitalConnectivityProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty

# Ground stations (inline, altitude in metres)
ground_obj = GroundObjectProperty(
    identifier="Ground Stations",
    type="ground_station",
    ground_objects=[
        GroundObjectData(id=0, label="Berlin", latitude=52.52, longitude=13.405, altitude=34.0),
        GroundObjectData(id=1, label="London", latitude=51.507, longitude=-0.127, altitude=11.0),
    ],
    connectivity_properties=GroundConnectivityProperty(
        ground_to_space_connections_strategy="best-angle-until-disconnection",
        elevation_above_horizon=10,
        maximum_satellite_range_distance=1500.0,
        shells_whitelist=["LEO"],
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

## Architecture

```
satgonetem/
  models/        Node, Satellite, GroundStation, Link, Interface
                 MPLS entries, routing entries, sat_com Pydantic models
  dynamics/      DynamicsModel (abstract)
  launchers/     NetworkLauncher (abstract) + GoNetEmLauncher (gRPC)
                 HILManager (hardware-in-the-loop)
  routing/       RoutingDaemon (abstract) + static, OSPF, ISIS-SR, SR-MPLS
  traffic/       Ping, iperf2, iperf3, hping3 traffic tools and FlowScheduler
  link_budget/   Antenna, transmitter, receiver, propagation, MODCOD, geometry
  addressing/    Deterministic IPv4 pools, node blocks, loopbacks, and /31 links
  services/      TopologyManager (central orchestrator)
                 mixins: topology sync, link ops, routing mgr, interface mgr,
                         network lifecycle, traffic testing, simulation loop, diagnostics
  utils/         project_builder, IP helpers, custom connection strategies
  proto/         gRPC definitions for GoNetem backend
```

**TopologyManager** (`satgonetem/services/topology_satcom.py`) is the main entry point. It is composed of mixins under `services/mixins/` that each handle a specific concern: syncing topology data from the orbital simulator, managing Docker containers and virtual interfaces, applying `tc` qdiscs, configuring routing daemons, running traffic tests, and driving the simulation tick loop.

**Delay model:** `delay_ms = distance_km / 299792.458 * 1000` (propagation at speed of light).

**QoS stack per link:** `netem` (propagation delay) + `TBF` (rate limiting), applied via batched `tc` commands using pyroute2.

## IPv4 Addressing

Addresses come from a deterministic, private `10.0.0.0/8` plan by default:

| Purpose | Pool | Per-node block | Capacity |
|---------|------|----------------|----------|
| Ground stations | `10.0.0.0/10` | `/20` | 1,024 |
| User terminals | `10.64.0.0/10` | `/20` | 1,024 |
| Satellites | `10.128.0.0/9` | `/27` | 262,144 |

Each node has a stable `/32` loopback at `<block>.0.1`; traffic and static routes target that loopback, not an interface. Every point-to-point link gets a `/31` starting at `<block>.0.4`. Ground stations and user terminals own their links; satellite links are owned by the lower satellite ID. See [`docs/addressing.md`](docs/addressing.md) for the link-ownership rules and configuration.

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
| `topology_events` | `False` | Experimental V5 EventBus lifecycle path; polling is faster at scale in V5.0.3 |
| `event_reconcile_interval` | 30 | Full link-scan reconciliation interval in ticks when events are enabled; 0 disables |
| `link_expiry_horizon_s` | `0` | Routing-penalty horizon for links predicted to disconnect soon; 0 disables |
| `link_expiry_penalty` | `0` | Extra Dijkstra weight for links inside the expiry horizon; 0 disables |
| `routing` | `"static"` | Routing method name |
| `isl_link_capacity` | - | Inter-satellite link capacity in kbps |
| `gnd_link_capacity` | - | Ground-space link capacity in kbps |
| `satellite_image` | - | Docker image used for satellite containers |
| `network_launcher` | `"DIRECT"` | `"GONETEM"` to use legacy gRPC backend |
| `ground_station_pool` | `10.0.0.0/10` | Pool subdivided into ground-station `/20` blocks |
| `satellite_pool` | `10.128.0.0/9` | Pool subdivided into satellite blocks |
| `user_terminal_pool` | `10.64.0.0/10` | Pool subdivided into user-terminal `/20` blocks |
| `ground_station_prefix_len` | 20 | Prefix length of each ground-station block |
| `satellite_prefix_len` | 27 | Prefix length of each satellite block |
| `user_terminal_prefix_len` | 20 | Prefix length of each user-terminal block |

## Testing

```bash
pytest tests/ -v
```

Tests cover models, routing daemons, traffic result parsing, launchers, and project builder utilities. Tests that exercise Docker or network operations require a suitable host environment.

Scale and live connectivity checks:

```bash
# Address-plan scale: 900 satellites, 5 GS, 20 UTs, 2,900 unique /31 links
python scripts/verify_900_satellites.py

# Live three-node topology: GSL + ISL, static routing, bidirectional pings
python scripts/verify_connectivity.py

# Same, when GoNetEm NodeRun cannot exec into containers in the environment
python scripts/verify_connectivity.py --transport docker
```

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
