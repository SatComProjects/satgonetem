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

## Features

- Real-time satellite constellation simulation (LEO/MEO/GEO, walker and TLE-based)
- Docker container-per-node with kernel-level netem delay and rate shaping
- Hardware-in-the-loop (HIL) support for bridging physical ground station equipment
- Pluggable routing backends: static (Dijkstra), OSPF (Bird2), IS-IS SR (FRR), SR-MPLS
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
git+https://github.com/SatComProjects/satComTopology.git
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
  services/      TopologyManager (central orchestrator)
                 mixins: topology sync, link ops, routing mgr, interface mgr,
                         network lifecycle, traffic testing, simulation loop, diagnostics
  utils/         project_builder, IP utilities, custom connection strategies
  proto/         gRPC definitions for GoNetem backend
```

**TopologyManager** (`satgonetem/services/topology_satcom.py`) is the main entry point. It is composed of mixins under `services/mixins/` that each handle a specific concern: syncing topology data from the orbital simulator, managing Docker containers and virtual interfaces, applying `tc` qdiscs, configuring routing daemons, running traffic tests, and driving the simulation tick loop.

**Delay model:** `delay_ms = distance_km / 299792.458 * 1000` (propagation at speed of light).

**QoS stack per link:** `netem` (propagation delay) + `TBF` (rate limiting), applied via batched `tc` commands using pyroute2.

## Routing Methods

| Method | Class | Description |
|--------|-------|-------------|
| `static` | StaticRoutingDaemon | Dijkstra over NetworkX graph, `ip route` applied per node |
| `dynamic-ospf` | OSPFDaemon | Bird2 daemon, per-node config templated via Jinja2 |
| `dynamic-isis` | ISISBirdSRDaemon | Bird2 IS-IS with SR-MPLS |
| `sr-mpls` | SRMPLSDaemon | Segment Routing with MPLS labels |

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
| `isl_link_capacity` | - | Inter-satellite link capacity in kbps |
| `gnd_link_capacity` | - | Ground-space link capacity in kbps |
| `satellite_image` | - | Docker image used for satellite containers |
| `network_launcher` | `"DIRECT"` | `"GONETEM"` to use legacy gRPC backend |

## Testing

```bash
pytest tests/ -v
```

Tests cover models, routing daemons, traffic result parsing, launchers, and project builder utilities. Tests that exercise Docker or network operations require a suitable host environment.

## License

MIT - see `pyproject.toml` for author and contact information.
