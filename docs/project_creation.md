# Project Creation

This guide covers how to build a satellite topology project entirely in Python, without
any YAML files on disk, using `SimulationProperty` and `TopologyManager.from_satcom`.

## Overview

```
SimulationProperty  (describes the constellation and ground objects)
    |
    +-- TopologyManager.from_satcom(project)
            |
            +-- satellites: {id: Satellite, ...}
            +-- ground_stations: {id: GroundStation, ...}
            +-- links: {frozenset: Link, ...}
```

`SimulationProperty` is imported directly from the `sat_com_topology` dependency
(`sat_com_builder.models`). It holds all constellation parameters in memory and
produces the configuration that `TopologyManager` needs to initialise a
`SimulationManager`. No files are required until the topology is launched.

---

## Quick start

```python
from satgonetem.utils.project_builder import create_test_project
from satgonetem.services.topology_satcom import TopologyManager

project = create_test_project()
topology = TopologyManager.from_satcom(project)

print(len(topology.satellites))       # number of satellites
print(len(topology.ground_stations))  # number of ground stations
print(len(topology.links))            # number of active links
```

`create_test_project` returns a `SimulationProperty` representing a pre-configured
Iridium-like Walker Star constellation (7 planes x 11 satellites, 86.4 deg
inclination) with five European ground stations. Use it to prototype or run tests
without writing any configuration code.

---

## Building a project from scratch

### 1. Define ground stations

```python
from satgonetem.utils.project_builder import GroundStationEntry, GroundObjectFile
from sat_com_builder.models import GroundConnectivityProperty, GroundObjectProperty

entries = [
    GroundStationEntry(index=0, name="Berlin",  latitude=52.52,  longitude=13.405, elevation_km=0.034),
    GroundStationEntry(index=1, name="London",  latitude=51.507, longitude=-0.127, elevation_km=0.011),
    GroundStationEntry(index=2, name="Madrid",  latitude=40.416, longitude=-3.703, elevation_km=0.667),
]

gs_file = GroundObjectFile("Ground Stations", entries)
data_file = gs_file.write("/tmp")

conn_props = GroundConnectivityProperty(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=10,
    maximum_satellite_range_distance=1500.0,
    shell_white_lists=["LEO"],
    maximum_connected_satellites=3,
)

ground_obj = GroundObjectProperty(
    identifier=gs_file.identifier,
    data_file=data_file,
    type="ground_station",
    connectivity_properties=conn_props,
)
```

If your stations are already in a file, you can also build `GroundObjectFile` from
CSV data:

```python
from satgonetem.utils.project_builder import GroundObjectFile

gs_file = GroundObjectFile.from_csv(
    identifier="Ground Stations",
    csv_path="resources/ground_station_files/representative.txt",
)
```

`from_csv` expects rows in this format (no header):
`index,name,latitude,longitude,elevation_km`.

`GroundObjectFile.identifier` becomes the key used in `ground_object_white_list` on the
shell. The identifier string must match exactly.

**Ground-to-space connection strategies**

| Strategy name                          | Description                                      |
|----------------------------------------|--------------------------------------------------|
| `best-angle-until-disconnection`       | Connect to the satellite with the best elevation angle; hold until the link drops |
| `best-range-until-disconnection`       | Connect to the nearest satellite; hold until the link drops |
| `best-multi-angle-until-disconnection` | Like best-angle but allows multiple simultaneous links |
| `longest-connection-time-strategy`     | Prefer satellites that will remain visible longest |
| `weighted-connection`                  | Score-based selection combining angle and range  |
| `everything-visible`                   | Connect to all satellites above the horizon      |
| `everything-in-range`                  | Connect to all satellites within range limit     |

### 2. Define the constellation shell

```python
from sat_com_builder.models import OrbitalConnectivityProperty, WalkerShellProperty
from sat_com_constellation.models import WalkerConstellationProperty

constellation = WalkerConstellationProperty(
    identifier="LEO",
    amount_of_orbit_plane=7,
    amount_of_satellite_per_orbit_plane=11,
    inclination=86.4,
    mean_revolution_per_day=14.35,
    phase_difference_between_satellites=True,
)

isl_props = OrbitalConnectivityProperty(
    adjacent_inter_satellite_shifting=0,
    maximum_inter_satellite_count=4,
    maximum_inter_satellite_range_distance=1500.0,
    maximum_ground_station_range=1200.0,
    maximum_user_terminal_range=1000.0,
    maximum_connected_ground_object=10000,
    maximum_connected_user_terminal=500,
    maximum_connected_ground_station=10,
)

shell = WalkerShellProperty(
    type="star",
    constellation_property=constellation,
    orbital_connectivity_property=isl_props,
    ground_object_white_list=["Ground Stations"],
)
```

`type` is either `"star"` (polar/near-polar orbits, same RAAN spacing) or `"delta"`
(inclined orbits with a delta pattern). `ground_object_white_list` must list the
identifiers of the `GroundObjectFile` objects that satellites in this shell can connect
to.

**WalkerConstellationProperty fields**

| Field | Type | Description |
|---|---|---|
| `identifier` | str | Shell name referenced by ground objects and links |
| `amount_of_orbit_plane` | int | Number of orbital planes |
| `amount_of_satellite_per_orbit_plane` | int | Satellites per plane |
| `inclination` | float | Orbital inclination in degrees |
| `mean_revolution_per_day` | float | Orbital revolutions per day |
| `phase_difference_between_satellites` | bool | Whether adjacent planes are phase-shifted |

**OrbitalConnectivityProperty fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `adjacent_inter_satellite_shifting` | int | `0` | Phase offset between adjacent planes for ISLs |
| `maximum_inter_satellite_count` | int | `4` | Max ISLs per satellite |
| `maximum_inter_satellite_range_distance` | float | `None` | Max ISL distance in km |
| `maximum_ground_station_range` | float | `None` | Max ground-station link distance in km |
| `maximum_user_terminal_range` | float | `None` | Max user-terminal link distance in km |
| `maximum_connected_ground_object` | int | `None` | Max ground objects per satellite |
| `maximum_connected_user_terminal` | int | `None` | Max user terminals per satellite |
| `maximum_connected_ground_station` | int | `None` | Max ground stations per satellite |

### 3. Assemble the project

```python
from sat_com_builder.models import SimulationProperty

project = SimulationProperty(
    simulation_name="MyConstellation",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 00:10:00",
    walker_shells=[shell],
    ground_objects_properties=[ground_obj],
)
```

**SimulationProperty fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `simulation_name` | str | required | Project name; also used as the gRPC project label |
| `start_date` | str | required | Simulation start in format `"DD/MM/YYYY HH:MM:SS"` |
| `end_date` | str | required | Simulation end in the same format |
| `walker_shells` | list | required | One or more `WalkerShellProperty` instances |
| `ground_objects_properties` | list | required | One or more `GroundObjectProperty` instances |
| `movement_model` | str | `"pyorbital"` | Orbital propagation backend |
| `distance_model` | str | `"sklearn"` | Distance computation backend |
| `disable_ground_station_link_preload` | bool | `False` | Disable link preload optimisation |
| `static_ground_station_link_mode` | bool | `False` | Freeze GSL topology after first compute |

### 4. Create the topology manager

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_satcom(project)
```

`from_satcom` calls `create_and_load_simulation` with the serialised
`SimulationProperty`, loads a `SimulationManager`, and passes it to
`TopologyManager.__init__`. No YAML file is written or read.

---

## Alternative: start from an existing SimulationManager

If you need to modify the simulation *before* the topology is built—for example,
to inject custom satellites, ground stations, or extra links, you can create the
`SimulationManager` yourself and pass it to `TopologyManager.from_simulation_manager`.

### Creating a SimulationManager

Use `create_and_load_simulation` from `satgonetem.utils.project_builder`:

```python
from satgonetem.utils.project_builder import create_and_load_simulation
from sat_com_builder.models import SimulationProperty

project = SimulationProperty(...)

sim_manager = create_and_load_simulation(
    dict_configuration=project.model_dump(),
    project_name=project.simulation_name,
)
```

`create_and_load_simulation` takes a plain Python dict matching the
`SimulationProperty` schema, validates it, and returns an initialised
`SimulationManager`.

### Building the TopologyManager from the simulation manager

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_simulation_manager(sim_manager)
```

`from_simulation_manager` skips the `create_and_load_simulation` step and uses
the `SimulationManager` you already have. This is useful when you want to:

* Add custom satellites or ground stations after loading the simulation but
  before building the network topology.
* Create a `SimulationManager` once and reuse it across multiple
  `TopologyManager` instances.
* Load a simulation from a different source (e.g. a saved JSON file) and
  manually reconstruct the manager.

**Important ordering constraint:** Any modifications to the simulation manager
(such as adding custom satellites, ground stations, or manual links) must be
done **before** calling `TopologyManager.from_simulation_manager`. Once the
`TopologyManager` is constructed it runs an initial sync that reads satellites,
ground stations, and links from the manager; changes made afterwards will not
be picked up automatically.

---

## Custom satellites

You can inject TLE-based satellites into a running `SimulationManager` before
creating the `TopologyManager`.

### Creating a custom satellite

```python
from satgonetem.utils.project_builder import create_custom_satellite

custom_sat = {
    "name": "ISS (ZARYA)",
    "tle_line1": "1 25544U 98067A   26126.19956580  .00006859  00000-0  13198-3 0  9993",
    "tle_line2": "2 25544  51.6304 147.9377 0007403  28.6982 331.4412 15.49115842565248",
}

new_id = create_custom_satellite(custom_sat, sim_manager)
```

`create_custom_satellite` builds a `PyOrbitalModel` from the TLE, wraps it in a
sat_com_model `Satellite`, adds it to the simulation manager, refreshes
ground-station links so the new satellite is considered for connections, and
returns the new satellite's `topology_uniq_id` as an `int`.

You can add multiple custom satellites by calling `create_custom_satellite`
repeatedly or by using the helper `add_custom_satellites`:

```python
from satgonetem.utils.project_builder import add_custom_satellites

custom_satellites = [
    {"name": "Sat1", "tle_line1": "...", "tle_line2": "..."},
    {"name": "Sat2", "tle_line1": "...", "tle_line2": "..."},
]
add_custom_satellites(sim_manager, custom_satellites)
```

After all custom satellites are added, create the topology manager:

```python
topology = TopologyManager.from_simulation_manager(sim_manager)
```

### Extra: fetch live ISS TLE data

The `satgonetem.utils.utils` module provides a small helper that fetches the
current ISS TLE from `https://live.ariss.org/iss.txt`:

```python
from satgonetem.utils.utils import fetch_iss_data

iss = fetch_iss_data()
print(iss["name"])        # ISS (ZARYA)
print(iss["tle_line1"])   # 1 25544U 98067A   ...
print(iss["tle_line2"])   # 2 25544  51.6304 ...
```

You can pass the result directly to `create_custom_satellite`:

```python
from satgonetem.utils.project_builder import create_custom_satellite
from satgonetem.utils.utils import fetch_iss_data

iss_data = fetch_iss_data()
new_id = create_custom_satellite(iss_data, sim_manager)
```

### Fetching multiple satellites from a URL

If you have a URL that serves TLE data for many satellites (three lines per
satellite: name, line 1, line 2), use `fetch_satellites_data`:

```python
from satgonetem.utils.utils import fetch_satellites_data
from satgonetem.utils.project_builder import add_custom_satellites

satellites = fetch_satellites_data("https://example.com/constellation.txt")
add_custom_satellites(sim_manager, satellites)
```

The returned list has the same shape used by `add_custom_satellites`:

```python
[
    {
        "name": "ONEWEB-0012",
        "tle_line1": "1 44057U 19010A   26126.83484127  .00000046  00000+0  86073-4 0  9992",
        "tle_line2": "2 44057  87.9096 236.9957 0002389 103.2080 256.9318 13.16595320346123",
    },
    ...
]
```

### Reading multiple satellites from a local file

For offline workflows you can read the same three-line format from a local
`.txt` file with `read_satellites_data`:

```python
from satgonetem.utils.utils import read_satellites_data
from satgonetem.utils.project_builder import add_custom_satellites

satellites = read_satellites_data("./tle_data/oneweb.txt")
add_custom_satellites(sim_manager, satellites)
```

---

## Custom ground stations and manual links

The same pattern applies to ground stations and manually defined links: create
them **after** the `SimulationManager` is loaded but **before** the
`TopologyManager` is created.

### Adding a custom ground station

```python
from satgonetem.utils.project_builder import (
    create_custom_ground_station,
    add_custom_ground_station,
)

# Create the ground station object
gs = create_custom_ground_station(
    name="CustomGS",
    latitude=48.856,
    longitude=2.352,
    elevation_km=0.035,
    object_id=100,
)

# Inject it into the simulation manager
add_custom_ground_station(gs, sim_manager)
```

`create_custom_ground_station` builds a sat_com_model `GroundStation` with a
fixed geographic position. `add_custom_ground_station` registers it in the
simulation manager.  Automatic ground-station links are **not** refreshed
automatically; if you want the manager to connect the station to visible
satellites based on the current strategy, set a `ground_object_domain` on the
station and call `simulation_manager.update_ground_station_links()` after all
stations have been added.

### Adding a manual inter-satellite link

```python
from satgonetem.utils.project_builder import create_and_add_inter_satellite_link

# Both satellites must already exist in the simulation manager
create_and_add_inter_satellite_link("Sat1", "Sat2", sim_manager)
```

Satellites are looked up by their `satellite_name`. The link is created as an
`InterSatelliteLink` and appended to the simulation.

### Adding a manual ground-station link

```python
from satgonetem.utils.project_builder import create_and_add_ground_station_link

create_and_add_ground_station_link("Sat1", "Gnd7", sim_manager)
```

The satellite is looked up by `satellite_name` and the ground station by its
`label`. The link is created as a `GroundStationLink` and appended to the
simulation.

### Adding a manual ground-object link

```python
from satgonetem.utils.project_builder import create_and_add_ground_object_link

# Connect two ground objects (ground stations, user terminals, etc.)
create_and_add_ground_object_link("Gnd7", "Gnd8", sim_manager)
```

Both endpoints are looked up by their `label` across all ground objects. The
link is created as a `GroundObjectLink` and appended to the simulation.  You
can also connect a ground station to a user terminal this way.

### Full example: modify then build

```python
from satgonetem.utils.project_builder import (
    create_and_load_simulation,
    create_custom_satellite,
    create_custom_ground_station,
    add_custom_ground_station,
    create_and_add_inter_satellite_link,
    create_and_add_ground_station_link,
    create_and_add_ground_object_link,
)
from satgonetem.services.topology_satcom import TopologyManager
from sat_com_builder.models import SimulationProperty

# 1. Load the simulation manager
project = SimulationProperty(...)
sim_manager = create_and_load_simulation(
    project.model_dump(), project.simulation_name
)

# 2. Add custom objects BEFORE creating TopologyManager
sat_id = create_custom_satellite({"name": "SatX", "tle_line1": "...", "tle_line2": "..."}, sim_manager)

gs = create_custom_ground_station("Gnd7", 48.856, 2.352, 0.035)
add_custom_ground_station(gs, sim_manager)

create_and_add_inter_satellite_link("SatX", "SatY", sim_manager)
create_and_add_ground_station_link("SatX", "Gnd7", sim_manager)
create_and_add_ground_object_link("Gnd7", "Gnd8", sim_manager)

# 3. Create the topology manager
topology = TopologyManager.from_simulation_manager(sim_manager)
```

---

## Overriding network configuration

Build the topology with `from_satcom` first, then apply non-default network parameters
with `_apply_network_config`:

```python
from satgonetem.services.topology_satcom import TopologyManager, NetworkConfig

topology = TopologyManager.from_satcom(project)

net_cfg = NetworkConfig(
    project_name="MyConstellation",
    update_time=5,
    isl_link_capacity=100000,
    gnd_link_capacity=50000,
    ground_object_link_capacity=50000,
    routing="static",
    satellite_image="jariassuarez/sgnt:satellite",
    network_launcher="GONETEM",
    gonetem_server="localhost:10110",
)

topology._apply_network_config(net_cfg)
```

**NetworkConfig fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `project_name` | str or None | `None` | Human-readable project name; falls back to simulation name |
| `update_time` | int | `5` | Topology tick interval in seconds |
| `gnd_link_capacity` | int | `100000` | Ground-station link capacity in kbps |
| `isl_link_capacity` | int | `100000` | Inter-satellite link capacity in kbps |
| `ground_object_link_capacity` | int | `100000` | Ground-object-to-ground-object link capacity in kbps |
| `protocol` | str | `"ipv4"` | Network-layer protocol |
| `routing` | str | `"static"` | Default routing method |
| `satellite_image` | str | `"jariassuarez/sgnt:satellite"` | Docker image for satellite containers |
| `network_launcher` | str | `"GONETEM"` | Emulation backend |
| `gonetem_server` | str | `"localhost:10110"` | GoNetem gRPC server address |
| `use_budget` | bool | `False` | Enable link-budget-based capacity calculation |

---

## Saving and loading a topology

A fully configured `TopologyManager` can be serialised to a single JSON file and
restored later with `from_file`. All ground station data files referenced in the
configuration are embedded in the JSON, so the file is self-contained.

### Saving

```python
topology.to_file("my_topology.json")
```

By default `to_file` captures the current `NetworkConfig` from the instance. Pass
an explicit `NetworkConfig` to override what is persisted:

```python
from satgonetem.services.topology_satcom import NetworkConfig

net_cfg = NetworkConfig(isl_link_capacity=500000, routing="dynamic-ospf")
topology.to_file("my_topology.json", network_config=net_cfg)
```

The file contains three top-level keys:

| Key | Contents |
|---|---|
| `simulation_property` | Serialised `SimulationProperty` (constellation, ground objects, dates) |
| `network_config` | Serialised `NetworkConfig` fields |
| `ground_files` | Map of original file path to embedded file content and basename |

### Loading

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_file("my_topology.json")
```

`from_file` writes any embedded ground station data files to `/tmp/<stem>_ground_files/`
(e.g. `/tmp/my_topology_ground_files/`), updates the `data_file` paths in the
configuration, then calls `from_satcom` and
applies the persisted `NetworkConfig`. The returned instance is ready to use in the
same way as one built directly with `from_satcom`.

---

## Accessing topology data

After construction, the three main collections are ready:

```python
# Iterate satellites
for sat_id, sat in topology.satellites.items():
    print(sat_id, sat.position)   # {"latitude": ..., "longitude": ..., "altitude": ...}

# Iterate ground stations
for gs_id, gs in topology.ground_stations.items():
    print(gs_id, gs.city, gs.position)

# Iterate links
for key, link in topology.links.items():
    print(link.type, link.is_active, link.delay, link.capacity)
```

Node IDs follow these conventions:

| Node type | ID prefix | Example |
|---|---|---|
| Satellite | `"Sat"` | `"Sat42"` |
| Ground station | `"Gnd"` | `"Gnd3"` |

---

## Multiple shells

Pass multiple `WalkerShellProperty` objects to model multi-orbit constellations. Each shell
must have a distinct `identifier` in its `WalkerConstellationProperty`. Ground objects
reference shells by that identifier via `shell_white_lists` (in `GroundConnectivityProperty`)
and `ground_object_white_list` (on `WalkerShellProperty`).

```python
leo_shell = WalkerShellProperty(
    type="star",
    constellation_property=WalkerConstellationProperty(identifier="LEO", ...),
    ...
)
meo_shell = WalkerShellProperty(
    type="delta",
    constellation_property=WalkerConstellationProperty(identifier="MEO", ...),
    ...
)

conn_props = GroundConnectivityProperty(
    shell_white_lists=["LEO", "MEO"],  # ground stations can connect to both shells
    ...
)

project = SimulationProperty(
    ...,
    walker_shells=[leo_shell, meo_shell],
    ground_objects_properties=[ground_obj],
)
```

---

## User terminals

User terminals are defined the same way as ground stations but with `type="user_terminal"`:

```python
ut_entries = [
    GroundStationEntry(0, "Terminal-A", 48.8, 2.3, 0.05),
]
ut_file = GroundObjectFile("User Terminals", ut_entries)
ut_conn = GroundConnectivityProperty(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=5,
    maximum_satellite_range_distance=1000.0,
    shell_white_lists=["LEO"],
    maximum_connected_satellites=1,
)
ut_obj = GroundObjectProperty(
    identifier=ut_file.identifier,
    data_file=ut_file.write("/tmp"),
    type="user_terminal",
    connectivity_properties=ut_conn,
)
```

The shell's `orbital_connectivity_property.maximum_user_terminal_range` and
`maximum_connected_user_terminal` fields control how satellites treat user terminal links.
